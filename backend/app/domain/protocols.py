from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, runtime_checkable
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


@runtime_checkable
class SessionDocumentRepository(Protocol):
    async def reserve_and_create(
        self, session_update: SessionRecord, session_etag: str, document: DocumentRecord
    ) -> tuple[SessionRecord, VersionedDocument]: ...


class DocumentRepository(Protocol):
    async def get(self, session_key: str, document_id: UUID) -> VersionedDocument | None: ...

    async def create(self, document: DocumentRecord) -> VersionedDocument: ...

    async def replace(self, document: DocumentRecord, etag: str) -> VersionedDocument: ...

    async def delete(self, session_key: str, document_id: UUID, etag: str) -> None: ...

    async def list_for_session(self, session_key: str) -> list[VersionedDocument]: ...

    async def list_lifecycle_candidates(
        self, now: datetime, limit: int
    ) -> list[VersionedDocument]: ...

    async def list_deleted_before(
        self, cutoff: datetime, limit: int
    ) -> list[VersionedDocument]: ...

    async def commit_queued_with_outbox(
        self, document: DocumentRecord, document_etag: str, outbox: OutboxRecord
    ) -> VersionedDocument: ...

    async def list_pending_outbox(self, limit: int) -> list[tuple[OutboxRecord, str]]: ...

    async def get_pending_outbox(self, outbox_id: str) -> tuple[OutboxRecord, str] | None: ...

    async def mark_outbox_sent(self, outbox_id: str, etag: str, sent_at: datetime) -> None: ...


class SessionRepository(Protocol):
    async def get(self, session_key: str) -> tuple[SessionRecord, str] | None: ...

    async def create(self, session: SessionRecord) -> tuple[SessionRecord, str]: ...

    async def replace(self, session: SessionRecord, etag: str) -> tuple[SessionRecord, str]: ...


@dataclass(frozen=True, slots=True)
class BlobUploadGrant:
    upload_url: str
    expires_at: datetime
    required_headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class OfficePackageSummary:
    entry_names: tuple[str, ...]
    entry_count: int
    total_uncompressed_bytes: int


@dataclass(frozen=True, slots=True)
class VerifiedBlobUpload:
    header: bytes
    package: bytes | None = None
    office_summary: OfficePackageSummary | None = None


class WorkQueue(Protocol):
    async def enqueue_ingestion(self, message: IngestionMessage) -> None: ...

    async def enqueue_result_cleanup(self, message: ContentResultCleanupMessage) -> None: ...


class IngestionBacklog(Protocol):
    async def get_ingestion_backlog(self) -> int: ...


class UploadBlobStore(Protocol):
    async def create_upload(self, blob_name: str, content_type: str) -> BlobUploadGrant: ...

    async def verify_upload(
        self,
        blob_name: str,
        expected_etag: str,
        expected_size: int,
        expected_content_type: str,
        *,
        office: bool,
    ) -> VerifiedBlobUpload: ...

    async def aclose(self) -> None: ...


class BlobStore(UploadBlobStore, Protocol):

    async def create_read_url(self, blob_name: str, expires_at: datetime) -> str: ...

    async def read_prefix(self, blob_name: str, length: int) -> bytes: ...

    async def delete(self, blob_name: str) -> None: ...

    def acquire_document_lease(
        self, session_key: str, document_id: UUID
    ) -> "DocumentLeaseContext": ...

    async def delete_document_artifacts(self, session_key: str, document_id: UUID) -> None: ...

    async def document_artifacts_exist(self, session_key: str, document_id: UUID) -> bool: ...


class DocumentLease(Protocol):
    def ensure_valid(self) -> None: ...


class DocumentLeaseContext(Protocol):
    async def __aenter__(self) -> DocumentLease: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class ContentUnderstandingClient(Protocol):
    async def start_analysis(self, blob_url: str, analyzer_id: str) -> tuple[str, str]: ...

    async def get_result(self, operation_url: str) -> Mapping[str, JsonValue]: ...

    async def delete_result(self, result_id: str) -> None: ...


class EmbeddingClient(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class ChunkSearch(Protocol):
    async def upsert(self, chunks: Sequence[DocumentChunk]) -> None: ...

    async def delete_for_document(self, session_key: str, document_id: UUID) -> None: ...

    async def has_for_document(self, session_key: str, document_id: UUID) -> bool: ...

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
