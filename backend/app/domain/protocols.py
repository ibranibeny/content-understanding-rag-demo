from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from app.domain.models import (
    ContentResultCleanupMessage,
    DocumentChunk,
    DocumentRecord,
    IngestionMessage,
    OutboxRecord,
    RetrievedEvidence,
    SessionRecord,
    VersionedDocument,
)


class DocumentRepository(Protocol):
    async def get(self, session_key: str, document_id: UUID) -> VersionedDocument | None: ...

    async def create(self, document: DocumentRecord) -> VersionedDocument: ...

    async def replace(self, document: DocumentRecord, etag: str) -> VersionedDocument: ...

    async def list_for_session(self, session_key: str) -> list[VersionedDocument]: ...

    async def commit_queued_with_outbox(
        self, document: DocumentRecord, document_etag: str, outbox: OutboxRecord
    ) -> VersionedDocument: ...

    async def list_pending_outbox(self, limit: int) -> list[tuple[OutboxRecord, str]]: ...

    async def mark_outbox_sent(self, outbox_id: str, etag: str) -> None: ...


class SessionRepository(Protocol):
    async def get(self, session_key: str) -> tuple[SessionRecord, str] | None: ...

    async def create(self, session: SessionRecord) -> tuple[SessionRecord, str]: ...

    async def replace(self, session: SessionRecord, etag: str) -> tuple[SessionRecord, str]: ...


class WorkQueue(Protocol):
    async def enqueue_ingestion(self, message: IngestionMessage) -> None: ...

    async def enqueue_result_cleanup(self, message: ContentResultCleanupMessage) -> None: ...


class BlobStore(Protocol):
    async def create_upload_url(
        self, blob_name: str, content_type: str, expires_at: datetime
    ) -> str: ...

    async def create_read_url(self, blob_name: str, expires_at: datetime) -> str: ...

    async def read_prefix(self, blob_name: str, length: int) -> bytes: ...

    async def delete(self, blob_name: str) -> None: ...


class ContentUnderstandingClient(Protocol):
    async def start_analysis(self, blob_url: str, analyzer_id: str) -> tuple[str, str]: ...

    async def get_result(self, operation_url: str) -> Mapping[str, JsonValue]: ...

    async def delete_result(self, result_id: str) -> None: ...


class EmbeddingClient(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class ChunkSearch(Protocol):
    async def upsert(self, chunks: Sequence[DocumentChunk]) -> None: ...

    async def delete_for_document(self, session_key: str, document_id: UUID) -> None: ...

    async def search(
        self, session_key: str, query: str, vector: Sequence[float], document_ids: Sequence[UUID]
    ) -> list[RetrievedEvidence]: ...


class ChatModel(Protocol):
    def stream(
        self, question: str, evidence: Sequence[RetrievedEvidence]
    ) -> AsyncIterator[str]: ...


class ReadinessCheck(Protocol):
    async def __call__(self) -> bool: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
