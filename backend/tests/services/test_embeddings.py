import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from azure.core.credentials import AccessToken

from app.services.embeddings import (
    EMBEDDING_DIMENSIONS,
    MAX_BATCH_SIZE,
    TOKEN_SCOPE,
    EmbeddingError,
    FoundryEmbeddingClient,
)

ENDPOINT = "https://demo.services.ai.azure.com"


class Credential:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    async def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        del kwargs
        self.calls.extend(scopes)
        expires = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
        return AccessToken("entra-token", expires)

    async def close(self) -> None:
        self.closed = True


def vector(value: float = 0.0) -> list[float]:
    return [value] * EMBEDDING_DIMENSIONS


async def test_uses_entra_only_fixed_deployment_dimensions_and_release_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": vector()}]})

    credential = Credential()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FoundryEmbeddingClient(
        ENDPOINT, credential=credential, http_client=http, release_sha="abc123"
    )

    result = await client.embed(["hello"])

    request = seen[0]
    assert request.url.path == "/openai/deployments/text-embedding-3-large/embeddings"
    assert request.headers["Authorization"] == "Bearer entra-token"
    assert "api-key" not in request.headers
    assert request.headers["x-ms-useragent"] == "content-understanding-rag/abc123"
    assert json.loads(request.content) == {"input": ["hello"], "dimensions": 3072}
    assert len(result[0]) == 3072
    assert credential.calls == [TOKEN_SCOPE]
    await http.aclose()


async def test_batches_at_most_64_and_preserves_input_order() -> None:
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        sizes.append(len(inputs))
        return httpx.Response(
            200,
            json={"data": [{"index": index, "embedding": vector(float(text))} for index, text in enumerate(inputs)]},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FoundryEmbeddingClient(ENDPOINT, credential=Credential(), http_client=http)
    values = [str(index) for index in range(MAX_BATCH_SIZE + 1)]

    result = await client.embed(values)

    assert sizes == [64, 1]
    assert [item[0] for item in result] == [float(index) for index in range(65)]
    await http.aclose()


async def test_rejects_input_above_8192_tokens_before_auth_or_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    credential = Credential()
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: requests.append(request)))
    client = FoundryEmbeddingClient(ENDPOINT, credential=credential, http_client=http)
    monkeypatch.setattr("app.services.embeddings.count_tokens", lambda text: 8193)

    with pytest.raises(EmbeddingError) as caught:
        await client.embed(["too large"])

    assert caught.value.code == "embedding_input_too_large"
    assert not caught.value.retryable
    assert requests == []
    assert credential.calls == []
    await http.aclose()


async def test_rejects_wrong_dimension_and_malformed_cardinality_safely() -> None:
    responses = iter([
        httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]}),
        httpx.Response(200, json={"data": []}),
    ])
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: next(responses)))
    client = FoundryEmbeddingClient(ENDPOINT, credential=Credential(), http_client=http)

    for _ in range(2):
        with pytest.raises(EmbeddingError) as caught:
            await client.embed(["private text"])
        assert caught.value.code == "embedding_malformed_response"
        assert "private text" not in str(caught.value)
    await http.aclose()


async def test_retries_429_and_5xx_using_retry_after_without_leaking_body() -> None:
    statuses = iter([429, 503, 200])
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        if status != 200:
            return httpx.Response(status, headers={"Retry-After": "0.25"}, text="private text")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": vector()}]})

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FoundryEmbeddingClient(
        ENDPOINT, credential=Credential(), http_client=http, sleep=sleep
    )

    assert len((await client.embed(["hello"]))[0]) == 3072
    assert sleeps == [0.25, 0.25]
    await http.aclose()


@pytest.mark.parametrize(("status", "retryable"), [(400, False), (401, False), (403, False), (500, True)])
async def test_exhausted_or_terminal_errors_are_safe(status: int, retryable: bool) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="secret response")
        )
    )
    client = FoundryEmbeddingClient(
        ENDPOINT, credential=Credential(), http_client=http, max_attempts=1
    )

    with pytest.raises(EmbeddingError) as caught:
        await client.embed(["private text"])
    assert caught.value.retryable is retryable
    assert "secret" not in str(caught.value)
    assert "private" not in repr(caught.value)
    await http.aclose()


async def test_close_respects_injected_and_owned_resources() -> None:
    credential = Credential()
    injected_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None))
    injected = FoundryEmbeddingClient(ENDPOINT, credential=credential, http_client=injected_http)
    await injected.aclose()
    assert not credential.closed
    assert not injected_http.is_closed

    owned_credential = Credential()
    owned_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None))
    owned = FoundryEmbeddingClient(
        ENDPOINT,
        credential=owned_credential,
        http_client=owned_http,
        owns_credential=True,
        owns_http_client=True,
    )
    await owned.aclose()
    assert owned_credential.closed
    assert owned_http.is_closed
    await injected_http.aclose()
