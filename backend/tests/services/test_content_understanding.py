import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from azure.core.credentials import AccessToken

from app.services.content_understanding import (
    API_VERSION,
    TOKEN_SCOPE,
    AnalysisStatus,
    ContentUnderstandingClient,
    ContentUnderstandingError,
)

ENDPOINT = "https://demo.services.ai.azure.com"
OPERATION = f"{ENDPOINT}/contentunderstanding/analyzerResults/result-1?api-version={API_VERSION}"
ANALYZERS = Path(__file__).parents[3] / "analyzers"
ANALYZER_PROPERTIES = {"baseAnalyzerId", "description", "config", "fieldSchema", "models"}


class Credential:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    async def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        del kwargs
        self.calls.extend(scopes)
        return AccessToken(f"token-{len(self.calls)}", int((datetime.now(UTC) + timedelta(hours=1)).timestamp()))

    async def close(self) -> None:
        self.closed = True


def client(handler: Any, credential: Credential | None = None) -> tuple[ContentUnderstandingClient, Credential, httpx.AsyncClient]:
    token = credential or Credential()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ContentUnderstandingClient(ENDPOINT, credential=token, http_client=http), token, http


def assert_token_only(request: httpx.Request, token: str = "token-1") -> None:
    assert request.headers["Authorization"] == f"Bearer {token}"
    assert "Ocp-Apim-Subscription-Key" not in request.headers


async def test_start_analysis_uses_exact_path_body_version_and_returns_persistable_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/contentunderstanding/analyzers/business_document_router:analyze"
        assert dict(request.url.params) == {"api-version": API_VERSION}
        assert request.read() == b'{"inputs":[{"url":"https://blob.example/file.pdf?sig=secret"}]}'
        assert_token_only(request)
        return httpx.Response(202, headers={"Operation-Location": OPERATION}, json={"id": "result-1", "status": "NotStarted"})

    service, credential, http = client(handler)
    started = await service.start_analysis("https://blob.example/file.pdf?sig=secret", "business_document_router")

    assert started.result_id == "result-1"
    assert started.operation_url == OPERATION
    assert credential.calls == [TOKEN_SCOPE]
    await service.aclose()
    assert not credential.closed
    assert not http.is_closed
    await http.aclose()


async def test_start_analysis_sends_exact_range_in_input() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.read() == (
            b'{"inputs":[{"url":"https://blob.example/file.pdf?sig=secret",'
            b'"range":"1-3,5"}]}'
        )
        return httpx.Response(202, headers={"Operation-Location": OPERATION})

    service, _, http = client(handler)

    await service.start_analysis(
        "https://blob.example/file.pdf?sig=secret",
        "business_document_router",
        "1-3,5",
    )

    await http.aclose()


async def test_start_analysis_accepts_header_only_202_response() -> None:
    service, _, http = client(
        lambda request: httpx.Response(202, headers={"Operation-Location": OPERATION})
    )

    started = await service.start_analysis("https://blob.example/file.pdf", "router")

    assert started.result_id == "result-1"
    assert started.operation_url == OPERATION
    await http.aclose()


async def test_start_analysis_ignores_mismatching_body_id() -> None:
    service, _, http = client(
        lambda request: httpx.Response(
            202,
            headers={"Operation-Location": OPERATION},
            json={"id": "attacker-controlled"},
        )
    )

    started = await service.start_analysis("https://blob.example/file.pdf", "router")

    assert started.result_id == "result-1"
    await http.aclose()


async def test_each_request_refreshes_token_and_owned_dependencies_close() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(204)

    credential = Credential()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = ContentUnderstandingClient(ENDPOINT, credential=credential, http_client=http, owns_http_client=True, owns_credential=True)
    await service.delete_result("one")
    await service.delete_result("two")
    await service.aclose()

    assert seen == ["Bearer token-1", "Bearer token-2"]
    assert http.is_closed
    assert credential.closed


async def test_untrusted_operation_location_is_rejected_before_request_or_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    service, credential, http = client(handler)
    with pytest.raises(ContentUnderstandingError) as caught:
        await service.poll("https://evil.example/contentunderstanding/analyzerResults/stolen?api-version=2025-11-01")

    assert not caught.value.retryable
    assert requests == []
    assert credential.calls == []
    await http.aclose()


@pytest.mark.parametrize("status", ["NotStarted", "Running"])
async def test_poll_reports_pending_statuses(status: str) -> None:
    service, _, http = client(lambda request: httpx.Response(200, json={"id": "result-1", "status": status}))
    result = await service.poll(OPERATION)
    assert result.status is AnalysisStatus(status)
    assert result.result is None
    await http.aclose()


async def test_poll_normalizes_document_result_fields_locators_pages_and_tokens() -> None:
    payload = {
        "id": "result-1",
        "status": "Succeeded",
        "usage": {"contextualizationTokens": 11, "tokens": {"gpt-5": {"input": 7, "output": 3}}},
        "result": {"contents": [{
            "kind": "document", "category": "invoice", "markdown": "# Invoice", "endPageNumber": 2,
            "pages": [{"pageNumber": 1}, {"pageNumber": 2}],
            "fields": {
                "vendorName": {"type": "string", "valueString": "Contoso", "source": "D(1,1,1,2,1,2,2,1,2)"},
                "total": {"type": "number", "valueNumber": 42.5},
            },
        }]},
    }
    service, _, http = client(lambda request: httpx.Response(200, json=payload))
    polled = await service.poll(OPERATION)

    assert polled.status is AnalysisStatus.SUCCEEDED
    assert polled.result is not None
    assert polled.result.category == "invoice"
    assert polled.result.markdown == "# Invoice"
    assert polled.result.fields == {"vendorName": "Contoso", "total": 42.5}
    assert polled.result.source_locators == {"vendorName": "D(1,1,1,2,1,2,2,1,2)"}
    assert polled.result.page_count == 2
    assert polled.result.token_counts == {"contextualizationTokens": 11, "tokens.gpt-5.input": 7, "tokens.gpt-5.output": 3}
    await http.aclose()


async def test_poll_counts_processed_pages_for_ranged_analysis() -> None:
    payload = {
        "id": "result-1",
        "status": "Succeeded",
        "result": {
            "contents": [
                {
                    "category": "invoice",
                    "markdown": "# Invoice",
                    "fields": {},
                    "pages": [{"pageNumber": 2}, {"pageNumber": 3}],
                    "endPageNumber": 3,
                }
            ]
        },
    }
    service, _, http = client(lambda request: httpx.Response(200, json=payload))

    polled = await service.poll(OPERATION)

    assert polled.result is not None
    assert polled.result.page_count == 2
    await http.aclose()


@pytest.mark.parametrize(
    "pages",
    [
        [],
        ["page 1"],
        [{}],
        [{"pageNumber": 0}],
        [{"pageNumber": True}],
        [{"pageNumber": 1}, {"pageNumber": 1}],
        [{"pageNumber": 2}, {"pageNumber": 1}],
    ],
    ids=[
        "empty",
        "non-dict-entry",
        "missing-page-number",
        "zero-page-number",
        "boolean-page-number",
        "duplicate-page-number",
        "out-of-order-page-number",
    ],
)
async def test_poll_rejects_malformed_page_metadata(pages: list[object]) -> None:
    payload = {
        "id": "result-1",
        "status": "Succeeded",
        "result": {
            "contents": [
                {
                    "category": "invoice",
                    "markdown": "# Invoice",
                    "fields": {},
                    "pages": pages,
                    "endPageNumber": 3,
                }
            ]
        },
    }
    service, _, http = client(lambda request: httpx.Response(200, json=payload))

    with pytest.raises(ContentUnderstandingError) as caught:
        await service.poll(OPERATION)

    assert caught.value.code == "content_understanding_malformed"
    assert not caught.value.retryable
    await http.aclose()


async def test_poll_uses_end_page_number_when_pages_are_absent() -> None:
    payload = {
        "id": "result-1",
        "status": "Succeeded",
        "result": {
            "contents": [
                {
                    "category": "invoice",
                    "markdown": "# Invoice",
                    "fields": {},
                    "endPageNumber": 3,
                }
            ]
        },
    }
    service, _, http = client(lambda request: httpx.Response(200, json=payload))

    polled = await service.poll(OPERATION)

    assert polled.result is not None
    assert polled.result.page_count == 3
    await http.aclose()


async def test_poll_normalizes_image_locator_as_one_page() -> None:
    payload = {"id": "result-1", "status": "Succeeded", "result": {"contents": [{"kind": "document", "category": "receipt", "markdown": "receipt", "mimeType": "image/jpeg", "pages": [{"pageNumber": 1}], "fields": {"merchantName": {"type": "string", "valueString": "Shop", "source": "D(1,0,0,10,0,10,10,0,10)"}}}]}}
    service, _, http = client(lambda request: httpx.Response(200, json=payload))
    result = await service.poll(OPERATION)
    assert result.result is not None
    assert result.result.page_count == 1
    assert result.result.source_locators["merchantName"].startswith("D(1,")
    await http.aclose()


async def test_delete_result_uses_exact_path_and_accepts_204() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/contentunderstanding/analyzerResults/result-1"
        assert dict(request.url.params) == {"api-version": API_VERSION}
        assert_token_only(request)
        return httpx.Response(204)

    service, _, http = client(handler)
    await service.delete_result("result-1")
    await http.aclose()


async def test_delete_result_accepts_not_found_as_idempotent_success() -> None:
    service, _, http = client(lambda request: httpx.Response(404))
    await service.delete_result("result-1")
    await http.aclose()


async def test_poll_does_not_accept_not_found() -> None:
    service, _, http = client(lambda request: httpx.Response(404))
    with pytest.raises(ContentUnderstandingError) as caught:
        await service.poll(OPERATION)
    assert not caught.value.retryable
    await http.aclose()


async def test_create_analyzer_and_update_defaults_use_exact_methods() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201 if request.method == "PUT" else 200, json={})

    service, _, http = client(handler)
    await service.create_or_replace_analyzer("workshop_invoice", {"baseAnalyzerId": "prebuilt-document"})
    await service.update_defaults({"gpt-5": "gpt-5", "text-embedding-3-large": "text-embedding-3-large"})

    assert [(r.method, r.url.path, dict(r.url.params)) for r in requests] == [
        ("PUT", "/contentunderstanding/analyzers/workshop_invoice", {"api-version": API_VERSION, "allowReplace": "true"}),
        ("PATCH", "/contentunderstanding/defaults", {"api-version": API_VERSION}),
    ]
    assert requests[1].headers["Content-Type"] == "application/merge-patch+json"
    assert json.loads(requests[1].content) == {"modelDeployments": {"gpt-5": "gpt-5", "text-embedding-3-large": "text-embedding-3-large"}}
    await http.aclose()


async def test_create_analyzer_filters_every_checked_in_definition_to_ga_properties() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201)

    service, _, http = client(handler)
    definitions = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(ANALYZERS.glob("*.json"))
    ]

    for definition in definitions:
        await service.create_or_replace_analyzer(definition["analyzerId"], definition)

    assert len(requests) == len(definitions)
    for request, definition in zip(requests, definitions, strict=True):
        request_body = json.loads(request.content)
        assert set(request_body) == set(definition) & ANALYZER_PROPERTIES
        assert "analyzerId" not in request_body
        assert "name" not in request_body
        assert request_body["baseAnalyzerId"] == definition["baseAnalyzerId"]
        assert request_body["description"] == definition["description"]
        assert request_body["config"] == definition["config"]
        assert request_body["fieldSchema"] == definition["fieldSchema"]

    await http.aclose()


async def test_create_analyzer_accepts_only_trusted_creation_operation_location() -> None:
    operation = f"{ENDPOINT}/contentunderstanding/analyzers/workshop_invoice/operations/create-1?api-version={API_VERSION}"
    service, _, http = client(
        lambda request: httpx.Response(201, headers={"Operation-Location": operation}, json={})
    )
    assert await service.create_or_replace_analyzer("workshop_invoice", {}) == operation
    await http.aclose()


async def test_analyzer_identifiers_follow_ga_service_pattern() -> None:
    service, _, http = client(lambda request: httpx.Response(201))

    with pytest.raises(ContentUnderstandingError) as caught:
        await service.create_or_replace_analyzer("workshop-invoice", {})

    assert caught.value.code == "content_understanding_invalid_identifier"
    await http.aclose()


async def test_wait_for_analyzer_polls_until_ready() -> None:
    operation = (
        f"{ENDPOINT}/contentunderstanding/analyzers/workshop_invoice/operations/create-1"
        f"?api-version={API_VERSION}"
    )
    responses = iter(
        [
            httpx.Response(200, json={"status": "running"}),
            httpx.Response(200, json={"status": "succeeded"}),
        ]
    )
    service, _, http = client(lambda request: next(responses))

    await service.wait_for_analyzer(operation, poll_interval=0)

    await http.aclose()


async def test_network_timeout_is_retryable_without_leaking_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("https://blob.example/file.pdf?sig=secret", request=request)

    service, _, http = client(handler)
    with pytest.raises(ContentUnderstandingError) as caught:
        await service.delete_result("result-1")
    assert caught.value.retryable
    assert "sig=secret" not in str(caught.value)
    await http.aclose()


@pytest.mark.parametrize(("status", "retryable"), [(408, True), (409, True), (429, True), (500, True), (503, True), (400, False), (401, False), (403, False)])
async def test_http_errors_are_safely_classified(status: int, retryable: bool) -> None:
    service, _, http = client(lambda request: httpx.Response(status, headers={"Retry-After": "7"}, text="secret document response"))
    with pytest.raises(ContentUnderstandingError) as caught:
        await service.delete_result("result-1")
    assert caught.value.retryable is retryable
    assert caught.value.retry_after == (7.0 if retryable else None)
    assert "secret document response" not in str(caught.value)
    assert "secret document response" not in repr(caught.value)
    await http.aclose()


async def test_failed_and_malformed_results_are_terminal_and_safe() -> None:
    responses = iter([
        httpx.Response(200, json={"id": "result-1", "status": "Failed", "error": {"message": "private content"}}),
        httpx.Response(200, json={"id": "result-1", "status": "Succeeded", "result": {"contents": []}}),
    ])
    service, _, http = client(lambda request: next(responses))
    for _ in range(2):
        with pytest.raises(ContentUnderstandingError) as caught:
            await service.poll(OPERATION)
        assert not caught.value.retryable
        assert "private content" not in str(caught.value)
    await http.aclose()
