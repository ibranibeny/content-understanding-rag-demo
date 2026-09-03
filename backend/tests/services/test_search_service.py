import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from app.core.errors import TransientArtifactError
from app.domain.models import DocumentChunk
from app.services.search_service import (
    SEARCH_INDEX_NAME,
    AzureSearchService,
    build_scope_filter,
    search_index_schema,
)

SESSION = "a" * 64
DOC_ID = UUID("9f4b8484-9f6b-44f2-b4d4-e5e7687c80df")
SCRIPT = Path(__file__).parents[3] / "scripts" / "search-index.json"


def chunk(ordinal: int = 0) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=f"chunk-{ordinal}",
        session_key=SESSION,
        document_id=DOC_ID,
        ordinal=ordinal,
        file_name="agreement.pdf",
        document_type="contract",
        title="Agreement",
        section_path="Terms",
        page_number=2,
        source_locator="page 2",
        content="Renewal is annual.",
        content_vector=tuple([0.25] * 3072),
        expires_at=datetime(2026, 9, 4, tzinfo=UTC),
    )


class AsyncResults:
    def __init__(self, values: list[dict[str, Any]]) -> None:
        self.values = values

    def __aiter__(self):  # type: ignore[no-untyped-def]
        async def iterate():  # type: ignore[no-untyped-def]
            for value in self.values:
                yield value
        return iterate()


class SearchClient:
    def __init__(self) -> None:
        self.uploads: list[list[dict[str, Any]]] = []
        self.deletes: list[list[dict[str, Any]]] = []
        self.searches: list[tuple[str | None, dict[str, Any]]] = []
        self.closed = 0
        self.search_results: list[list[dict[str, Any]]] = [[]]
        self.upload_results: list[Any] | None = None
        self.delete_results: list[Any] | None = None

    async def merge_or_upload_documents(self, documents: list[dict[str, Any]]) -> list[Any]:
        self.uploads.append(documents)
        return self.upload_results or [SimpleNamespace(succeeded=True, key=item["chunkId"], error_message=None) for item in documents]

    async def delete_documents(self, documents: list[dict[str, Any]]) -> list[Any]:
        self.deletes.append(documents)
        return self.delete_results or [SimpleNamespace(succeeded=True, key=item["chunkId"], error_message=None) for item in documents]

    async def search(self, search_text: str | None = None, **kwargs: Any) -> AsyncResults:
        self.searches.append((search_text, kwargs))
        return AsyncResults(self.search_results.pop(0))

    async def close(self) -> None:
        self.closed += 1


class IndexClient:
    def __init__(self) -> None:
        self.indexes: list[Any] = []
        self.closed = 0
        self.names = [SEARCH_INDEX_NAME]

    async def create_or_update_index(self, index: Any) -> Any:
        self.indexes.append(index)
        return index

    def get_index_names(self) -> AsyncResults:
        return AsyncResults([{"name": name} for name in self.names])

    async def close(self) -> None:
        self.closed += 1


def service(search: SearchClient | None = None, index: IndexClient | None = None) -> AzureSearchService:
    return AzureSearchService(
        "https://search.example.com",
        SEARCH_INDEX_NAME,
        credential=object(),  # type: ignore[arg-type]
        search_client=search or SearchClient(),  # type: ignore[arg-type]
        index_client=index or IndexClient(),  # type: ignore[arg-type]
    )


def test_canonical_schema_exactly_matches_checked_in_keyless_index() -> None:
    expected = json.loads(SCRIPT.read_text(encoding="utf-8"))

    assert search_index_schema() == expected
    assert expected["name"] == "document-chunks"
    assert "apiKey" not in json.dumps(expected)
    assert expected["fields"][0] == {"name": "chunkId", "type": "Edm.String", "key": True}
    vector = next(field for field in expected["fields"] if field["name"] == "contentVector")
    assert vector["dimensions"] == 3072
    assert vector["vectorSearchProfile"] == "chunk-vector-profile"
    assert expected["vectorSearch"]["algorithms"][0]["hnswParameters"]["metric"] == "cosine"


async def test_create_or_update_uses_search_index_client_with_exact_design() -> None:
    index = IndexClient()
    search_service = service(index=index)

    await search_service.create_or_update_index()

    actual = index.indexes[0]
    assert actual.name == "document-chunks"
    assert [field.name for field in actual.fields] == [field["name"] for field in search_index_schema()["fields"]]
    assert actual.semantic_search.configurations[0].prioritized_fields.title_field.field_name == "title"


def test_scope_filter_is_mandatory_and_escapes_only_server_values() -> None:
    document = UUID("11111111-1111-1111-1111-111111111111")
    assert build_scope_filter("owner's-session", ()) == "sessionKey eq 'owner''s-session'"
    assert build_scope_filter("owner", (document,)) == (
        "sessionKey eq 'owner' and (documentId eq '11111111-1111-1111-1111-111111111111')"
    )
    with pytest.raises(ValueError, match="session"):
        build_scope_filter("", ())


async def test_upsert_batches_and_checks_every_partial_result() -> None:
    search = SearchClient()
    search_service = service(search=search)
    await search_service.upsert([chunk(index) for index in range(1001)])
    assert [len(batch) for batch in search.uploads] == [1000, 1]
    assert search.uploads[0][0]["sessionKey"] == SESSION
    assert search.uploads[0][0]["expiresAt"] == "2026-09-04T00:00:00Z"

    search.upload_results = [SimpleNamespace(succeeded=False, key="chunk-0", error_message="private")]
    with pytest.raises(TransientArtifactError, match="Search indexing failed") as caught:
        await search_service.upsert([chunk()])
    assert "private" not in str(caught.value)

    search.upload_results = [SimpleNamespace(succeeded=True, key="chunk-0", error_message=None)]
    with pytest.raises(TransientArtifactError, match="Search indexing failed"):
        await search_service.upsert([chunk(0), chunk(1)])


async def test_delete_enumerates_all_pages_then_deletes_key_batches() -> None:
    search = SearchClient()
    search.search_results = [[{"chunkId": f"key-{index}"} for index in range(1001)]]
    search_service = service(search=search)

    await search_service.delete_for_document("owner's-session", DOC_ID)

    _, args = search.searches[0]
    assert args["filter"] == (
        "sessionKey eq 'owner''s-session' and "
        "documentId eq '9f4b8484-9f6b-44f2-b4d4-e5e7687c80df'"
    )
    assert args["select"] == ["chunkId"]
    assert [len(batch) for batch in search.deletes] == [1000, 1]


async def test_has_for_document_uses_server_filter_and_no_client_filtering() -> None:
    search = SearchClient()
    search.search_results = [[{"chunkId": "one"}]]
    search_service = service(search=search)

    assert await search_service.has_for_document(SESSION, DOC_ID)
    assert search.searches[0][1]["top"] == 1
    assert search.searches[0][1]["filter"].startswith(f"sessionKey eq '{SESSION}'")


async def test_hybrid_search_uses_bm25_vector_semantic_top8_and_normalizes_results() -> None:
    search = SearchClient()
    search.search_results = [[{
        "chunkId": "chunk-0", "documentId": str(DOC_ID), "fileName": "agreement.pdf",
        "sourceLocator": "page 2", "content": "Renewal is annual.",
        "@search.score": 0.75, "@search.reranker_score": 3.5,
    }]]
    search_service = service(search=search)

    results = await search_service.search(SESSION, "renewal", [0.5] * 3072, [DOC_ID])

    text, args = search.searches[0]
    assert text == "renewal"
    assert args["query_type"] == "semantic"
    assert args["semantic_configuration_name"] == "chunk-semantic-config"
    assert args["top"] == 8
    assert args["filter"].startswith(f"sessionKey eq '{SESSION}'")
    vector_query = args["vector_queries"][0]
    assert vector_query.k_nearest_neighbors == 50
    assert vector_query.fields == "contentVector"
    assert results[0].citation_id == "S1"
    assert results[0].search_score == 0.75
    assert results[0].reranker_score == 3.5


async def test_search_rejects_wrong_vector_dimension_before_sdk_call() -> None:
    search = SearchClient()
    search_service = service(search=search)
    with pytest.raises(ValueError, match="3072"):
        await search_service.search(SESSION, "query", [0.0], [])
    assert search.searches == []


async def test_readiness_and_close_ownership() -> None:
    search, index = SearchClient(), IndexClient()
    search_service = service(search=search, index=index)
    assert await search_service.is_ready()
    await search_service.aclose()
    assert search.closed == 0
    assert index.closed == 0

    owned = AzureSearchService(
        "https://search.example.com",
        SEARCH_INDEX_NAME,
        credential=object(),  # type: ignore[arg-type]
        search_client=search,  # type: ignore[arg-type]
        index_client=index,  # type: ignore[arg-type]
        owns_clients=True,
    )
    await owned.aclose()
    assert search.closed == 1
    assert index.closed == 1
