from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, Self
from urllib.parse import quote, urlsplit

import httpx
from azure.identity.aio import DefaultAzureCredential

from app.services.chunking import count_tokens

TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
API_VERSION = "2024-10-21"
EMBEDDING_DEPLOYMENT = "text-embedding-3-large"
EMBEDDING_DIMENSIONS = 3072
MAX_BATCH_SIZE = 64
MAX_INPUT_TOKENS = 8192
MAX_REQUEST_TOKENS = 8192


class AsyncTokenCredential(Protocol):
    async def get_token(self, *scopes: str, **kwargs: Any) -> Any: ...

    async def close(self) -> None: ...


class EmbeddingError(Exception):
    """Safe embedding failure without request or response content."""

    def __init__(self, code: str, *, retryable: bool, retry_after: float | None = None) -> None:
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(code)


class FoundryEmbeddingClient:
    def __init__(
        self,
        endpoint: str,
        *,
        deployment: str = EMBEDDING_DEPLOYMENT,
        credential: AsyncTokenCredential | None = None,
        http_client: httpx.AsyncClient | None = None,
        release_sha: str = "local",
        max_attempts: int = 5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        owns_credential: bool | None = None,
        owns_http_client: bool | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("Foundry endpoint must be a root HTTPS URL")
        if deployment != EMBEDDING_DEPLOYMENT:
            raise ValueError(f"embedding deployment must be {EMBEDDING_DEPLOYMENT}")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._url = (
            f"{endpoint.rstrip('/')}/openai/deployments/{quote(deployment, safe='')}/embeddings"
        )
        self._credential = credential or DefaultAzureCredential()
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        self._release_sha = release_sha
        self._max_attempts = max_attempts
        self._sleep = sleep
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

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        token_counts = [count_tokens(text) for text in texts]
        if any(count > MAX_INPUT_TOKENS for count in token_counts):
            raise EmbeddingError("embedding_input_too_large", retryable=False)

        vectors: list[list[float]] = []
        batch: list[str] = []
        batch_tokens = 0
        for text, tokens in zip(texts, token_counts, strict=True):
            if batch and (len(batch) >= MAX_BATCH_SIZE or batch_tokens + tokens > MAX_REQUEST_TOKENS):
                vectors.extend(await self._embed_batch(batch))
                batch = []
                batch_tokens = 0
            batch.append(text)
            batch_tokens += tokens
        if batch:
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        response: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            token = await self._credential.get_token(TOKEN_SCOPE)
            try:
                response = await self._http.post(
                    self._url,
                    params={"api-version": API_VERSION},
                    headers={
                        "Authorization": f"Bearer {token.token}",
                        "x-ms-useragent": f"content-understanding-rag/{self._release_sha}",
                    },
                    json={"input": texts, "dimensions": EMBEDDING_DIMENSIONS},
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 == self._max_attempts:
                    raise EmbeddingError("embedding_unavailable", retryable=True) from exc
                await self._sleep(float(2**attempt))
                continue
            if response.status_code == 200:
                return self._vectors(response, len(texts))
            retryable = response.status_code == 429 or response.status_code >= 500
            retry_after = self._retry_after(response)
            if not retryable or attempt + 1 == self._max_attempts:
                raise EmbeddingError(
                    "embedding_unavailable" if retryable else "embedding_rejected",
                    retryable=retryable,
                    retry_after=retry_after,
                )
            await self._sleep(retry_after if retry_after is not None else float(2**attempt))
        raise EmbeddingError("embedding_unavailable", retryable=True)

    @staticmethod
    def _vectors(response: httpx.Response, expected: int) -> list[list[float]]:
        try:
            body = response.json()
            data = body["data"]
            ordered = sorted(data, key=lambda item: item["index"])
            vectors = [list(map(float, item["embedding"])) for item in ordered]
        except (ValueError, TypeError, KeyError) as exc:
            raise EmbeddingError("embedding_malformed_response", retryable=False) from exc
        if len(vectors) != expected or any(len(vector) != EMBEDDING_DIMENSIONS for vector in vectors):
            raise EmbeddingError("embedding_malformed_response", retryable=False)
        return vectors

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            delay = float(value)
        except ValueError:
            return None
        return min(delay, 60.0) if delay >= 0 else None
