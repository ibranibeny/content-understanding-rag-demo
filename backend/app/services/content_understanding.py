from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, Self, cast
from urllib.parse import parse_qs, quote, urlsplit

import httpx
from azure.identity.aio import DefaultAzureCredential

API_VERSION = "2025-11-01"
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
ANALYZER_DEFINITION_PROPERTIES = frozenset(
    {"baseAnalyzerId", "description", "config", "fieldSchema", "models"}
)


class AsyncTokenCredential(Protocol):
    async def get_token(self, *scopes: str, **kwargs: Any) -> Any: ...

    async def close(self) -> None: ...


class AnalysisStatus(StrEnum):
    NOT_STARTED = "NotStarted"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"


@dataclass(frozen=True, slots=True)
class AnalysisStart:
    result_id: str
    operation_url: str


@dataclass(frozen=True, slots=True)
class NormalizedAnalysis:
    category: str
    markdown: str
    fields: Mapping[str, Any]
    source_locators: Mapping[str, str]
    page_count: int
    token_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class PollResult:
    status: AnalysisStatus
    result_id: str
    result: NormalizedAnalysis | None = None


class ContentUnderstandingError(Exception):
    """A safe error that never includes service response content."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(code)


class ContentUnderstandingClient:
    def __init__(
        self,
        endpoint: str,
        *,
        credential: AsyncTokenCredential | None = None,
        http_client: httpx.AsyncClient | None = None,
        owns_credential: bool | None = None,
        owns_http_client: bool | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("Content Understanding endpoint must be a root HTTPS URL")
        self._endpoint = endpoint.rstrip("/")
        self._origin = (parsed.scheme, parsed.hostname.lower(), parsed.port or 443)
        self._credential = credential or DefaultAzureCredential()
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_credential = credential is None if owns_credential is None else owns_credential
        self._owns_http = http_client is None if owns_http_client is None else owns_http_client

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
        if self._owns_credential:
            await self._credential.close()

    async def create_or_replace_analyzer(
        self, analyzer_id: str, definition: Mapping[str, Any]
    ) -> str | None:
        request_body = {
            key: value
            for key, value in definition.items()
            if key in ANALYZER_DEFINITION_PROPERTIES
        }
        response = await self._request(
            "PUT",
            f"/contentunderstanding/analyzers/{quote(self._identifier(analyzer_id), safe='')}",
            params={"api-version": API_VERSION, "allowReplace": "true"},
            json=request_body,
            expected={200, 201},
        )
        operation = response.headers.get("Operation-Location")
        if operation is not None:
            self._validate_operation_url(operation, analyzer_operation=True)
        return str(operation) if operation is not None else None

    async def update_defaults(self, model_deployments: Mapping[str, str]) -> None:
        await self._request(
            "PATCH",
            "/contentunderstanding/defaults",
            params={"api-version": API_VERSION},
            headers={"Content-Type": "application/merge-patch+json"},
            json={"modelDeployments": dict(model_deployments)},
            expected={200},
        )

    async def start_analysis(self, blob_url: str, analyzer_id: str) -> AnalysisStart:
        response = await self._request(
            "POST",
            f"/contentunderstanding/analyzers/{quote(self._identifier(analyzer_id), safe='')}:analyze",
            params={"api-version": API_VERSION},
            json={"inputs": [{"url": blob_url}]},
            expected={202},
        )
        operation_url = response.headers.get("Operation-Location")
        if operation_url is None:
            raise ContentUnderstandingError("content_understanding_malformed", retryable=False)
        result_id = self._validate_operation_url(operation_url)
        return AnalysisStart(result_id=result_id, operation_url=operation_url)

    async def poll(self, operation_url: str) -> PollResult:
        result_id = self._validate_operation_url(operation_url)
        response = await self._request("GET", operation_url, expected={200})
        body = self._json_object(response)
        if body.get("id") != result_id:
            raise ContentUnderstandingError("content_understanding_malformed", retryable=False)
        raw_status = body.get("status")
        if not isinstance(raw_status, str):
            raise ContentUnderstandingError("content_understanding_malformed", retryable=False)
        try:
            status = AnalysisStatus(raw_status)
        except ValueError as exc:
            raise ContentUnderstandingError(
                "content_understanding_malformed", retryable=False
            ) from exc
        if status is AnalysisStatus.FAILED:
            raise ContentUnderstandingError("content_understanding_failed", retryable=False)
        if status is not AnalysisStatus.SUCCEEDED:
            return PollResult(status=status, result_id=result_id)
        return PollResult(
            status=status,
            result_id=result_id,
            result=self._normalize_result(body),
        )

    async def get_result(self, operation_url: str) -> Mapping[str, Any]:
        polled = await self.poll(operation_url)
        if polled.result is None:
            return {"id": polled.result_id, "status": polled.status.value}
        return {
            "id": polled.result_id,
            "status": polled.status.value,
            "category": polled.result.category,
            "markdown": polled.result.markdown,
            "fields": dict(polled.result.fields),
            "sourceLocators": dict(polled.result.source_locators),
            "pageCount": polled.result.page_count,
            "tokenCounts": dict(polled.result.token_counts),
        }

    async def delete_result(self, result_id: str) -> None:
        await self._request(
            "DELETE",
            f"/contentunderstanding/analyzerResults/{quote(self._identifier(result_id), safe='')}",
            params={"api-version": API_VERSION},
            expected={204},
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        expected: set[int],
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        token = await self._credential.get_token(TOKEN_SCOPE)
        request_headers = {"Authorization": f"Bearer {token.token}"}
        if headers:
            request_headers.update(headers)
        target = url if url.startswith("https://") else f"{self._endpoint}{url}"
        try:
            response = await self._http.request(
                method, target, params=params, headers=request_headers, json=json
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ContentUnderstandingError(
                "content_understanding_unavailable", retryable=True
            ) from exc
        if response.status_code not in expected:
            retryable = response.status_code in {408, 409, 429} or response.status_code >= 500
            raise ContentUnderstandingError(
                "content_understanding_unavailable" if retryable else "content_understanding_rejected",
                retryable=retryable,
                retry_after=self._retry_after(response) if retryable else None,
            )
        return response

    def _validate_operation_url(self, url: str, *, analyzer_operation: bool = False) -> str:
        parsed = urlsplit(url)
        origin = (parsed.scheme, (parsed.hostname or "").lower(), parsed.port or 443)
        if origin != self._origin or parsed.username is not None or parsed.password is not None:
            raise ContentUnderstandingError("content_understanding_untrusted_operation", retryable=False)
        query = parse_qs(parsed.query, strict_parsing=True)
        if query != {"api-version": [API_VERSION]} or parsed.fragment:
            raise ContentUnderstandingError("content_understanding_untrusted_operation", retryable=False)
        segments = parsed.path.split("/")
        if analyzer_operation:
            valid = (
                len(segments) == 6
                and segments[1:3] == ["contentunderstanding", "analyzers"]
                and segments[4] == "operations"
            )
            identifier = segments[-1] if valid else ""
        else:
            valid = len(segments) == 4 and segments[1:3] == ["contentunderstanding", "analyzerResults"]
            identifier = segments[-1] if valid else ""
        if not valid:
            raise ContentUnderstandingError("content_understanding_untrusted_operation", retryable=False)
        return self._identifier(identifier)

    @staticmethod
    def _identifier(value: str) -> str:
        if not value or len(value) > 128 or any(not (c.isalnum() or c in "._-") for c in value):
            raise ContentUnderstandingError("content_understanding_invalid_identifier", retryable=False)
        return value

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise ContentUnderstandingError("content_understanding_malformed", retryable=False) from exc
        if not isinstance(body, dict):
            raise ContentUnderstandingError("content_understanding_malformed", retryable=False)
        return cast(dict[str, Any], body)

    @classmethod
    def _normalize_result(cls, body: Mapping[str, Any]) -> NormalizedAnalysis:
        result = body.get("result")
        contents = result.get("contents") if isinstance(result, dict) else None
        if not isinstance(contents, list) or len(contents) != 1 or not isinstance(contents[0], dict):
            raise ContentUnderstandingError("content_understanding_malformed", retryable=False)
        content = contents[0]
        category, markdown, fields = content.get("category"), content.get("markdown"), content.get("fields")
        if not isinstance(category, str) or not isinstance(markdown, str) or not isinstance(fields, dict):
            raise ContentUnderstandingError("content_understanding_malformed", retryable=False)
        values: dict[str, Any] = {}
        locators: dict[str, str] = {}
        for name, raw in fields.items():
            if not isinstance(name, str) or not isinstance(raw, dict):
                raise ContentUnderstandingError("content_understanding_malformed", retryable=False)
            values[name] = cls._field_value(raw)
            source = raw.get("source")
            if isinstance(source, str):
                locators[name] = source
        pages = content.get("pages")
        page_count = len(pages) if isinstance(pages, list) else 0
        end_page = content.get("endPageNumber")
        if isinstance(end_page, int):
            page_count = max(page_count, end_page)
        return NormalizedAnalysis(
            category=category,
            markdown=markdown,
            fields=values,
            source_locators=locators,
            page_count=page_count,
            token_counts=cls._flatten_token_counts(body.get("usage")),
        )

    @classmethod
    def _field_value(cls, field: Mapping[str, Any]) -> Any:
        field_type = field.get("type")
        if not isinstance(field_type, str):
            raise ContentUnderstandingError("content_understanding_malformed", retryable=False)
        key = {
            "string": "valueString", "date": "valueDate", "time": "valueTime",
            "number": "valueNumber", "integer": "valueInteger", "boolean": "valueBoolean",
            "json": "valueJson", "array": "valueArray", "object": "valueObject",
        }.get(field_type)
        if key is None or key not in field:
            raise ContentUnderstandingError("content_understanding_malformed", retryable=False)
        value = field[key]
        if field_type == "array":
            if not isinstance(value, list):
                raise ContentUnderstandingError("content_understanding_malformed", retryable=False)
            return [cls._field_value(item) if isinstance(item, dict) and "type" in item else item for item in value]
        if field_type == "object":
            if not isinstance(value, dict):
                raise ContentUnderstandingError("content_understanding_malformed", retryable=False)
            return {name: cls._field_value(item) if isinstance(item, dict) and "type" in item else item for name, item in value.items()}
        return value

    @staticmethod
    def _flatten_token_counts(usage: Any) -> dict[str, int]:
        flattened: dict[str, int] = {}
        if not isinstance(usage, dict):
            return flattened

        def visit(prefix: str, value: Any) -> None:
            if isinstance(value, int) and not isinstance(value, bool):
                flattened[prefix] = value
            elif isinstance(value, dict):
                for key, nested in value.items():
                    if isinstance(key, str):
                        visit(f"{prefix}.{key}" if prefix else key, nested)

        visit("", usage)
        return flattened

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            return None
        return seconds if seconds >= 0 else None
