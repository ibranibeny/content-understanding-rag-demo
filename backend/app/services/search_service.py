from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, Self, cast
from uuid import UUID

from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError
from azure.identity.aio import DefaultAzureCredential
from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes.aio import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    HnswParameters,
    SearchableField,
    SearchField,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)
from azure.search.documents.models import VectorizedQuery

from app.core.errors import TransientArtifactError
from app.domain.models import DocumentChunk, RetrievedEvidence

SEARCH_INDEX_NAME = "document-chunks"
VECTOR_DIMENSIONS = 3072
VECTOR_PROFILE = "chunk-vector-profile"
SEMANTIC_CONFIGURATION = "chunk-semantic-config"
BATCH_SIZE = 1000


class SearchClientLike(Protocol):
    async def merge_or_upload_documents(self, documents: list[dict[str, Any]]) -> list[Any]: ...

    async def delete_documents(self, documents: list[dict[str, Any]]) -> list[Any]: ...

    async def search(self, search_text: str | None = None, **kwargs: Any) -> AsyncIterator[dict[str, Any]]: ...

    async def close(self) -> None: ...


class SearchIndexClientLike(Protocol):
    async def create_or_update_index(self, index: SearchIndex) -> SearchIndex: ...

    def get_index_names(self) -> AsyncIterator[str]: ...

    async def close(self) -> None: ...


def search_index_schema() -> dict[str, Any]:
    return {
        "name": SEARCH_INDEX_NAME,
        "fields": [
            {"name": "chunkId", "type": "Edm.String", "key": True},
            {"name": "sessionKey", "type": "Edm.String", "filterable": True},
            {"name": "documentId", "type": "Edm.String", "filterable": True},
            {"name": "fileName", "type": "Edm.String", "searchable": True, "retrievable": True},
            {"name": "documentType", "type": "Edm.String", "filterable": True, "facetable": True},
            {"name": "title", "type": "Edm.String", "searchable": True, "retrievable": True},
            {"name": "sectionPath", "type": "Edm.String", "searchable": True, "retrievable": True},
            {"name": "pageNumber", "type": "Edm.Int32", "filterable": True, "retrievable": True},
            {"name": "sourceLocator", "type": "Edm.String", "retrievable": True},
            {"name": "content", "type": "Edm.String", "searchable": True, "retrievable": True},
            {
                "name": "contentVector", "type": "Collection(Edm.Single)", "searchable": True,
                "retrievable": False, "dimensions": VECTOR_DIMENSIONS,
                "vectorSearchProfile": VECTOR_PROFILE,
            },
            {"name": "expiresAt", "type": "Edm.DateTimeOffset", "filterable": True, "sortable": True},
        ],
        "vectorSearch": {
            "algorithms": [{"name": "chunk-hnsw", "kind": "hnsw", "hnswParameters": {"metric": "cosine"}}],
            "profiles": [{"name": VECTOR_PROFILE, "algorithm": "chunk-hnsw"}],
        },
        "semantic": {
            "configurations": [{
                "name": SEMANTIC_CONFIGURATION,
                "prioritizedFields": {
                    "titleField": {"fieldName": "title"},
                    "prioritizedKeywordsFields": [{"fieldName": "sectionPath"}],
                    "prioritizedContentFields": [{"fieldName": "content"}],
                },
            }],
        },
    }


def build_search_index(name: str = SEARCH_INDEX_NAME) -> SearchIndex:
    fields = [
        SimpleField(name="chunkId", type="Edm.String", key=True),
        SimpleField(name="sessionKey", type="Edm.String", filterable=True),
        SimpleField(name="documentId", type="Edm.String", filterable=True),
        SearchableField(name="fileName", type="Edm.String"),
        SimpleField(name="documentType", type="Edm.String", filterable=True, facetable=True),
        SearchableField(name="title", type="Edm.String"),
        SearchableField(name="sectionPath", type="Edm.String"),
        SimpleField(name="pageNumber", type="Edm.Int32", filterable=True),
        SimpleField(name="sourceLocator", type="Edm.String"),
        SearchableField(name="content", type="Edm.String"),
        SearchField(
            name="contentVector", type="Collection(Edm.Single)",
            searchable=True, hidden=True, vector_search_dimensions=VECTOR_DIMENSIONS,
            vector_search_profile_name=VECTOR_PROFILE,
        ),
        SimpleField(name="expiresAt", type="Edm.DateTimeOffset", filterable=True, sortable=True),
    ]
    return SearchIndex(
        name=name,
        fields=fields,
        vector_search=VectorSearch(
            algorithms=[HnswAlgorithmConfiguration(name="chunk-hnsw", parameters=HnswParameters(metric="cosine"))],
            profiles=[VectorSearchProfile(name=VECTOR_PROFILE, algorithm_configuration_name="chunk-hnsw")],
        ),
        semantic_search=SemanticSearch(configurations=[SemanticConfiguration(
            name=SEMANTIC_CONFIGURATION,
            prioritized_fields=SemanticPrioritizedFields(
                title_field=SemanticField(field_name="title"),
                keywords_fields=[SemanticField(field_name="sectionPath")],
                content_fields=[SemanticField(field_name="content")],
            ),
        )]),
    )


def escape_odata(value: str) -> str:
    return value.replace("'", "''")


def build_scope_filter(session_key: str, document_ids: Sequence[UUID]) -> str:
    if not session_key:
        raise ValueError("session key is required")
    clauses = [f"sessionKey eq '{escape_odata(session_key)}'"]
    if document_ids:
        ids = " or ".join(
            f"documentId eq '{escape_odata(str(value))}'" for value in document_ids
        )
        clauses.append(f"({ids})")
    return " and ".join(clauses)


def _document_filter(session_key: str, document_id: UUID) -> str:
    return (
        f"sessionKey eq '{escape_odata(session_key)}' and "
        f"documentId eq '{escape_odata(str(document_id))}'"
    )


class AzureSearchService:
    def __init__(
        self,
        endpoint: str,
        index_name: str = SEARCH_INDEX_NAME,
        *,
        credential: AsyncTokenCredential | None = None,
        search_client: SearchClientLike | None = None,
        index_client: SearchIndexClientLike | None = None,
        owns_clients: bool | None = None,
        owns_credential: bool | None = None,
    ) -> None:
        self._credential = credential or DefaultAzureCredential()
        self._search = search_client or cast(
            SearchClientLike, SearchClient(endpoint, index_name, self._credential)
        )
        self._indexes = index_client or cast(
            SearchIndexClientLike, SearchIndexClient(endpoint, self._credential)
        )
        self._index_name = index_name
        self._owns_clients = (
            search_client is None and index_client is None
            if owns_clients is None else owns_clients
        )
        self._owns_credential = credential is None if owns_credential is None else owns_credential

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_clients:
            await self._search.close()
            await self._indexes.close()
        if self._owns_credential:
            await self._credential.close()

    async def create_or_update_index(self) -> None:
        await self._indexes.create_or_update_index(build_search_index(self._index_name))

    async def is_ready(self) -> bool:
        try:
            async for value in self._indexes.get_index_names():
                name = value.get("name") if isinstance(value, Mapping) else value
                if name == self._index_name:
                    return True
            return False
        except AzureError:
            return False

    async def upsert(self, chunks: Sequence[DocumentChunk]) -> None:
        documents = [self._chunk_document(chunk) for chunk in chunks]
        for batch in _batches(documents):
            results = await self._search.merge_or_upload_documents(batch)
            self._require_success(results, len(batch), "Search indexing failed.")

    async def delete_for_document(self, session_key: str, document_id: UUID) -> None:
        keys = await self._keys_for_document(session_key, document_id)
        for batch in _batches([{"chunkId": key} for key in keys]):
            results = await self._search.delete_documents(batch)
            self._require_success(results, len(batch), "Search deletion failed.")

    async def has_for_document(self, session_key: str, document_id: UUID) -> bool:
        results = await self._search.search(
            "*", filter=_document_filter(session_key, document_id), select=["chunkId"], top=1
        )
        async for _ in results:
            return True
        return False

    async def search(
        self,
        session_key: str,
        query: str,
        vector: Sequence[float],
        document_ids: Sequence[UUID],
    ) -> list[RetrievedEvidence]:
        if len(vector) != VECTOR_DIMENSIONS:
            raise ValueError("search vector must contain exactly 3072 values")
        results = await self._search.search(
            query,
            filter=build_scope_filter(session_key, document_ids),
            vector_queries=[VectorizedQuery(vector=list(vector), k_nearest_neighbors=50, fields="contentVector")],
            query_type="semantic",
            semantic_configuration_name=SEMANTIC_CONFIGURATION,
            select=["chunkId", "documentId", "fileName", "sourceLocator", "content"],
            top=8,
        )
        evidence: list[RetrievedEvidence] = []
        async for item in results:
            try:
                evidence.append(RetrievedEvidence(
                    citation_id=f"S{len(evidence) + 1}",
                    document_id=UUID(str(item["documentId"])),
                    chunk_id=str(item["chunkId"]),
                    file_name=str(item["fileName"]),
                    source_locator=str(item["sourceLocator"]),
                    content=str(item["content"]),
                    search_score=_optional_float(item.get("@search.score")),
                    reranker_score=_optional_float(item.get("@search.reranker_score")),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise TransientArtifactError("Search returned an invalid result.") from exc
        return evidence

    async def _keys_for_document(self, session_key: str, document_id: UUID) -> list[str]:
        results = await self._search.search(
            "*", filter=_document_filter(session_key, document_id), select=["chunkId"], top=None
        )
        keys: list[str] = []
        async for item in results:
            key = item.get("chunkId")
            if not isinstance(key, str):
                raise TransientArtifactError("Search returned an invalid result.")
            keys.append(key)
        return keys

    @staticmethod
    def _chunk_document(chunk: DocumentChunk) -> dict[str, Any]:
        return {
            "chunkId": chunk.chunk_id, "sessionKey": chunk.session_key,
            "documentId": str(chunk.document_id), "fileName": chunk.file_name,
            "documentType": chunk.document_type, "title": chunk.title,
            "sectionPath": chunk.section_path, "pageNumber": chunk.page_number,
            "sourceLocator": chunk.source_locator, "content": chunk.content,
            "contentVector": list(chunk.content_vector),
            "expiresAt": _utc_iso(chunk.expires_at),
        }

    @staticmethod
    def _require_success(results: Sequence[Any], expected: int, message: str) -> None:
        if len(results) != expected or any(
            not bool(getattr(result, "succeeded", False)) for result in results
        ):
            raise TransientArtifactError(message)


def _batches(values: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [values[start : start + BATCH_SIZE] for start in range(0, len(values), BATCH_SIZE)]


def _utc_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
