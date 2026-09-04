import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from app.core.errors import AppError, ConcurrencyConflict, TransientArtifactError
from app.domain.models import DocumentRecord, DocumentState, VersionedDocument
from app.domain.protocols import BlobStore, ChunkSearch, DocumentLease, DocumentRepository
from app.services.blob_service import DocumentLeaseBusy, DocumentLeaseLost

TOMBSTONE_RETENTION = timedelta(hours=48)
MAX_ETAG_RETRIES = 5


class TombstonedDocument(Exception):
    """A worker was fenced because the document is deleting or deleted."""


@dataclass(frozen=True, slots=True)
class SweepResult:
    deleted: int = 0
    pending: int = 0
    skipped: int = 0


class DeletionService:
    """Linearizes deletion with a tombstone and fences physical cleanup with a Blob lease."""

    def __init__(
        self,
        repository: DocumentRepository,
        blobs: BlobStore,
        search: ChunkSearch,
    ) -> None:
        self._repository = repository
        self._blobs = blobs
        self._search = search

    @staticmethod
    def _not_found() -> AppError:
        return AppError("document_not_found", 404, "The document was not found.", False)

    async def request_delete(
        self, session_key: str, document_id: UUID, now: datetime
    ) -> VersionedDocument:
        for _ in range(MAX_ETAG_RETRIES):
            current = await self._repository.get(session_key, document_id)
            if current is None:
                raise self._not_found()
            if current.value.state in {DocumentState.DELETING, DocumentState.DELETED}:
                return current
            tombstone = current.value.model_copy(
                update={
                    "state": DocumentState.DELETING,
                    "tombstoned_at": now,
                    "deletion_requested_at": now,
                    "updated_at": now,
                }
            )
            try:
                return await self._repository.replace(tombstone, current.etag)
            except ConcurrencyConflict:
                continue
        raise AppError(
            "document_update_conflict",
            409,
            "The document changed. Retry the request.",
            True,
        )

    async def guard_not_tombstoned(
        self, session_key: str, document_id: UUID, lease: DocumentLease
    ) -> VersionedDocument:
        lease.ensure_valid()
        current = await self._repository.get(session_key, document_id)
        if current is None or current.value.state in {
            DocumentState.DELETING,
            DocumentState.DELETED,
        }:
            raise TombstonedDocument
        return current

    @asynccontextmanager
    async def acquire_write_lease(
        self, session_key: str, document_id: UUID
    ) -> AsyncIterator[DocumentLease]:
        current = await self._repository.get(session_key, document_id)
        if current is None or current.value.state in {
            DocumentState.DELETING,
            DocumentState.DELETED,
        }:
            raise TombstonedDocument
        async with self._blobs.acquire_document_lease(session_key, document_id) as lease:
            await self.guard_not_tombstoned(session_key, document_id, lease)
            yield lease
            lease.ensure_valid()

    async def sweep_pending(self, now: datetime, limit: int) -> SweepResult:
        deleted = 0
        pending = 0
        skipped = 0
        candidates: list[VersionedDocument] = []
        continuation: str | None = None
        seen_continuations: set[str] = set()
        while len(candidates) < limit:
            page, following = await self._repository.list_lifecycle_candidates(
                now, min(100, limit - len(candidates)), continuation
            )
            candidates.extend(page)
            if following is None or following in seen_continuations or not page:
                break
            seen_continuations.add(following)
            continuation = following
        for candidate in candidates:
            if candidate.value.state is not DocumentState.DELETING:
                try:
                    candidate = await self.request_delete(
                        candidate.value.session_key, candidate.value.document_id, now
                    )
                except AppError:
                    pending += 1
                    continue
            outcome = await self._delete_one(candidate.value.session_key, candidate.value.document_id, now)
            if outcome == "deleted":
                deleted += 1
            elif outcome == "pending":
                pending += 1
            else:
                skipped += 1
        return SweepResult(deleted=deleted, pending=pending, skipped=skipped)

    async def _delete_one(self, session_key: str, document_id: UUID, now: datetime) -> str:
        try:
            async with self._blobs.acquire_document_lease(session_key, document_id) as lease:
                lease.ensure_valid()
                current = await self._repository.get(session_key, document_id)
                if current is None or current.value.state is DocumentState.DELETED:
                    return "skipped"
                if current.value.state is not DocumentState.DELETING:
                    return "skipped"
                lease.ensure_valid()
                await self._blobs.delete_document_artifacts(session_key, document_id)
                lease.ensure_valid()
                await self._search.delete_for_document(session_key, document_id)
                lease.ensure_valid()
                refreshed = await self._repository.get(session_key, document_id)
                if refreshed is None or refreshed.value.state is not DocumentState.DELETING:
                    return "skipped"
            refreshed = await self._repository.get(session_key, document_id)
            if refreshed is None or refreshed.value.state is not DocumentState.DELETING:
                return "skipped"
            await self._blobs.delete_control_blob(session_key, document_id)
            latest = await self._repository.get(session_key, document_id)
            if latest is None or latest.value.state is not DocumentState.DELETING:
                return "skipped"
            deleted = self._deleted_record(latest.value, now)
            try:
                await self._repository.replace(deleted, latest.etag)
            except ConcurrencyConflict:
                return "pending"
            return "deleted"
        except asyncio.CancelledError:
            raise
        except (DocumentLeaseBusy, DocumentLeaseLost, TransientArtifactError):
            return "pending"

    @staticmethod
    def _deleted_record(record: DocumentRecord, now: datetime) -> DocumentRecord:
        return record.model_copy(
            update={
                "state": DocumentState.DELETED,
                "updated_at": now,
                "file_name": None,
                "content_type": None,
                "content_range": None,
                "size_bytes": None,
                "blob_name": None,
                "document_type": None,
                "title": None,
                "content_result_id": None,
                "content_operation_url": None,
                "extraction": None,
                "markdown_blob_name": None,
                "page_count": None,
                "chunk_count": None,
                "token_count": None,
                "failure_code": None,
                "failure_retryable": False,
                "deleted_at": now,
            }
        )

    async def purge_deleted(self, now: datetime, limit: int) -> int:
        purged = 0
        for candidate in await self._repository.list_deleted_before(
            now - TOMBSTONE_RETENTION, limit
        ):
            session_key = candidate.value.session_key
            document_id = candidate.value.document_id
            try:
                if await self._blobs.document_artifacts_exist(session_key, document_id):
                    continue
                if await self._search.has_for_document(session_key, document_id):
                    continue
                current = await self._repository.get(session_key, document_id)
                if (
                    current is None
                    or current.value.state is not DocumentState.DELETED
                    or current.value.deleted_at is None
                    or current.value.deleted_at > now - TOMBSTONE_RETENTION
                ):
                    continue
                await self._repository.delete(session_key, document_id, current.etag)
                purged += 1
            except asyncio.CancelledError:
                raise
            except (ConcurrencyConflict, TransientArtifactError):
                continue
        return purged


__all__ = [
    "DeletionService",
    "DocumentLeaseBusy",
    "DocumentLeaseLost",
    "SweepResult",
    "TombstonedDocument",
]
