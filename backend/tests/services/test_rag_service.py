from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx

from app.domain.models import DocumentRecord, DocumentState, RetrievedEvidence
from app.repositories.memory_repository import MemoryDocumentRepository
from app.services.rag_service import FoundryGPT5Client, RagService

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
SESSION = "a" * 64
DOC_A = UUID("11111111-1111-4111-8111-111111111111")
DOC_B = UUID("22222222-2222-4222-8222-222222222222")
FIXTURE = Path(__file__).parents[1] / "fixtures" / "prompt_injection.md"


class Clock:
    def now(self) -> datetime:
        return NOW


class Embeddings:
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        assert len(texts) == 1
        return [[0.0] * 3072]


class Search:
    def __init__(self, results: list[RetrievedEvidence]) -> None:
        self.results = results
        self.calls: list[tuple[str, tuple[UUID, ...]]] = []

    async def search(
        self,
        session_key: str,
        query: str,
        vector: Sequence[float],
        document_ids: Sequence[UUID],
    ) -> list[RetrievedEvidence]:
        assert session_key == SESSION
        assert len(vector) == 3072
        self.calls.append((query, tuple(document_ids)))
        return self.results


class Model:
    def __init__(self, text: str = "The renewal is twelve months [S1].") -> None:
        self.text = text
        self.prompts: list[tuple[str, str]] = []
        self.closed = 0

    async def stream(self, instructions: str, input_text: str) -> AsyncIterator[str]:
        self.prompts.append((instructions, input_text))
        yield self.text

    async def aclose(self) -> None:
        self.closed += 1


def document(
    document_id: UUID,
    *,
    state: DocumentState = DocumentState.READY,
    expires_at: datetime = NOW + timedelta(hours=1),
) -> DocumentRecord:
    return DocumentRecord(
        session_key=SESSION,
        document_id=document_id,
        file_name="agreement.pdf",
        content_type="application/pdf",
        size_bytes=10,
        blob_name=f"uploads/{SESSION}/{document_id}/agreement.pdf",
        state=state,
        created_at=NOW,
        updated_at=NOW,
        expires_at=expires_at,
    )


def evidence(document_id: UUID, content: str = "Renewal is twelve months.") -> RetrievedEvidence:
    return RetrievedEvidence(
        citation_id="search-owned-id",
        document_id=document_id,
        chunk_id=f"chunk-{document_id}",
        file_name="agreement.pdf",
        source_locator="page 2",
        content=content,
        search_score=0.8,
        reranker_score=3.4,
    )


async def service_with(
    results: list[RetrievedEvidence],
    *records: DocumentRecord,
    model_text: str = "The renewal is twelve months [S1].",
) -> tuple[RagService, Search, Model, MemoryDocumentRepository]:
    repository = MemoryDocumentRepository()
    for record in records:
        await repository.create(record)
    search = Search(results)
    model = Model(model_text)
    return RagService(repository, Embeddings(), search, model, Clock()), search, model, repository


async def collect(service: RagService, document_ids: tuple[UUID, ...] = ()) -> list[Any]:
    return [
        event
        async for event in service.stream(
            question="What changed?", session_key=SESSION, document_ids=document_ids
        )
    ]


async def test_search_keeps_mandatory_session_scope_and_deleting_evidence_is_excluded() -> None:
    service, search, model, _ = await service_with(
        [evidence(DOC_A), evidence(DOC_B)],
        document(DOC_A),
        document(DOC_B, state=DocumentState.DELETING),
    )

    events = await collect(service)

    assert search.calls == [("What changed?", ())]
    assert "chunk-22222222" not in model.prompts[0][1]
    assert [event.type for event in events] == ["retrieval", "token", "citation", "done"]
    assert events[0].sources[0].citation_id == "S1"


async def test_selected_documents_must_be_owned_ready_and_nonexpired() -> None:
    service, search, _, _ = await service_with(
        [evidence(DOC_A)], document(DOC_A), document(DOC_B, state=DocumentState.DELETING)
    )

    events = await collect(service, (DOC_A, DOC_B))

    assert search.calls == [("What changed?", (DOC_A,))]
    assert events[-1].type == "done"


async def test_unknown_model_citation_is_dropped() -> None:
    service, _, _, _ = await service_with(
        [evidence(DOC_A)], document(DOC_A), model_text="Grounded [S1], invented [S99]."
    )
    events = await collect(service)
    assert [event.citation.citation_id for event in events if event.type == "citation"] == ["S1"]


async def test_prompt_injection_fixture_is_delimited_as_untrusted_data() -> None:
    injected = FIXTURE.read_text(encoding="utf-8")
    service, _, model, _ = await service_with([evidence(DOC_A, injected)], document(DOC_A))
    await collect(service)
    instructions, input_text = model.prompts[0]
    assert "PWNED" not in instructions
    assert "untrusted" in input_text.lower()
    assert injected in input_text
    assert "<evidence id=\"S1\">" in input_text


async def test_no_evidence_returns_explicit_insufficient_answer_without_model_call() -> None:
    service, _, model, _ = await service_with([], document(DOC_A))
    events = await collect(service)
    assert [event.type for event in events] == ["retrieval", "token", "done"]
    assert events[1].text == "I don't have enough evidence in the selected documents to answer."
    assert model.prompts == []


class Credential:
    def __init__(self) -> None:
        self.scopes: list[str] = []

    async def get_token(self, scope: str) -> Any:
        self.scopes.append(scope)
        return SimpleNamespace(token="entra-token")

    async def close(self) -> None:
        return None


async def test_gpt5_responses_stream_uses_entra_bearer_and_exact_deployment() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = request.content.decode()
        assert '"model":"gpt-5"' in body
        assert '"stream":true' in body
        assert '"store":false' in body
        assert '"effort":"medium"' in body
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
                'data: {"type":"response.completed","response":{"usage":{"input_tokens":3,'
                '"output_tokens":1}}}\n\n'
                'data: [DONE]\n\n'
            ),
        )

    credential = Credential()
    client = FoundryGPT5Client(
        "https://demo.openai.azure.com",
        credential=credential,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert [part async for part in client.stream("Ground only.", "Question")] == ["Hello"]
    assert credential.scopes == ["https://ai.azure.com/.default"]
    assert requests[0].url == "https://demo.openai.azure.com/openai/v1/responses"
    assert requests[0].headers["authorization"] == "Bearer entra-token"
    assert "api-key" not in requests[0].headers