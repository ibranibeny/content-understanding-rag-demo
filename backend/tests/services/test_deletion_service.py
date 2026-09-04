import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.core.errors import AppError, ConcurrencyConflict, TransientArtifactError
from app.domain.models import DocumentRecord, DocumentState, VersionedDocument
from app.repositories.memory_repository import MemoryDocumentRepository
from app.services.deletion_service import (
    DeletionService,
    DocumentLeaseBusy,
    DocumentLeaseLost,
    TombstonedDocument,
)

SESSION = "a" * 64
OTHER_SESSION = "b" * 64
DOC = UUID("9f4b8484-9f6b-44f2-b4d4-e5e7687c80df")
NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)


def record(
    *,
    session: str = SESSION,
    state: DocumentState = DocumentState.READY,
    expires_at: datetime = NOW + timedelta(hours=1),
    deleted_at: datetime | None = None,
) -> DocumentRecord:
    deleted = state is DocumentState.DELETED
    return DocumentRecord(
        session_key=session,
        document_id=DOC,
        file_name=None if deleted else "private-invoice.pdf",
        content_type=None if deleted else "application/pdf",
        size_bytes=None if deleted else 100,
        blob_name=None if deleted else f"uploads/{session}/{DOC}/private-invoice.pdf",
        state=state,
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(minutes=1),
        expires_at=expires_at,
        document_type=None if deleted else "invoice",
        title=None if deleted else "Private invoice",
        content_result_id=None if deleted else "result-private",
        content_operation_url=None if deleted else "https://example.test/private-operation",
        extraction=None if deleted else {"private": "content"},
        markdown_blob_name=None if deleted else f"derived/{session}/{DOC}/content.md",
        page_count=None if deleted else 2,
        chunk_count=None if deleted else 3,
        token_count=None if deleted else 50,
        failure_code=None if deleted else "private-failure",
        failure_retryable=not deleted,
        deleted_at=deleted_at,
    )


class Lease:
    def __init__(self) -> None:
        self.valid = True
        self.checks = 0

    def ensure_valid(self) -> None:
        self.checks += 1
        if not self.valid:
            raise DocumentLeaseLost


class Blobs:
    def __init__(self) -> None:
        self.busy = False
        self.error: Exception | None = None
        self.present = True
        self.delete_calls: list[tuple[str, UUID]] = []
        self.control_delete_calls: list[tuple[str, UUID]] = []
        self.lease = Lease()
        self.on_acquire = None

    @asynccontextmanager
    async def acquire_document_lease(
        self, session_key: str, document_id: UUID
    ) -> AsyncIterator[Lease]:
        if self.busy:
            raise DocumentLeaseBusy
        if self.on_acquire is not None:
            await self.on_acquire()
        yield self.lease

    async def delete_document_artifacts(self, session_key: str, document_id: UUID) -> None:
        self.delete_calls.append((session_key, document_id))
        if self.error is not None:
            raise self.error
        self.present = False

    async def document_artifacts_exist(self, session_key: str, document_id: UUID) -> bool:
        del session_key, document_id
        return self.present

    async def delete_control_blob(self, session_key: str, document_id: UUID) -> None:
        self.control_delete_calls.append((session_key, document_id))


class Search:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.present = True
        self.delete_calls: list[tuple[str, UUID]] = []

    async def delete_for_document(self, session_key: str, document_id: UUID) -> None:
        self.delete_calls.append((session_key, document_id))
        if self.error is not None:
            raise self.error
        self.present = False

    async def has_for_document(self, session_key: str, document_id: UUID) -> bool:
        del session_key, document_id
        return self.present


async def make_service(
    document: DocumentRecord | None = None,
) -> tuple[DeletionService, MemoryDocumentRepository, Blobs, Search]:
    repository = MemoryDocumentRepository()
    if document is not None:
        await repository.create(document)
    blobs = Blobs()
    search = Search()
    return DeletionService(repository, blobs, search), repository, blobs, search


async def test_request_delete_tombstones_before_any_physical_side_effect() -> None:
    service, repository, blobs, search = await make_service(record())

    accepted = await service.request_delete(SESSION, DOC, NOW)

    assert accepted.value.state is DocumentState.DELETING
    assert accepted.value.tombstoned_at == NOW
    assert accepted.value.deletion_requested_at == NOW
    assert blobs.delete_calls == []
    assert search.delete_calls == []
    assert (await repository.get(SESSION, DOC)).value.state is DocumentState.DELETING  # type: ignore[union-attr]


async def test_request_delete_retries_an_etag_race_and_is_idempotent() -> None:
    service, repository, _, _ = await make_service(record())
    original_replace = repository.replace
    calls = 0

    async def race(document: DocumentRecord, etag: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConcurrencyConflict
        return await original_replace(document, etag)

    repository.replace = race  # type: ignore[method-assign]
    first = await service.request_delete(SESSION, DOC, NOW)
    second = await service.request_delete(SESSION, DOC, NOW + timedelta(minutes=1))

    assert calls == 2
    assert second == first
    assert second.value.deletion_requested_at == NOW


async def test_cross_session_and_missing_documents_are_same_safe_not_found() -> None:
    service, _, _, _ = await make_service(record())
    for session, document_id in ((OTHER_SESSION, DOC), (SESSION, UUID(int=0))):
        with pytest.raises(AppError) as caught:
            await service.request_delete(session, document_id, NOW)
        assert (caught.value.code, caught.value.status_code) == ("document_not_found", 404)
        assert "private" not in caught.value.message.lower()


async def test_busy_writer_keeps_deleting_then_next_sweep_completes_and_clears_content() -> None:
    service, repository, blobs, search = await make_service(record())
    await service.request_delete(SESSION, DOC, NOW)
    blobs.busy = True

    pending = await service.sweep_pending(NOW, 10)
    assert (pending.deleted, pending.pending) == (0, 1)
    assert (await repository.get(SESSION, DOC)).value.state is DocumentState.DELETING  # type: ignore[union-attr]

    blobs.busy = False
    completed = await service.sweep_pending(NOW + timedelta(minutes=1), 10)
    current = (await repository.get(SESSION, DOC)).value  # type: ignore[union-attr]
    assert (completed.deleted, completed.pending) == (1, 0)
    assert current.state is DocumentState.DELETED
    assert current.deleted_at == NOW + timedelta(minutes=1)
    assert current.extraction is None
    assert current.content_result_id is None
    assert current.content_operation_url is None
    assert current.markdown_blob_name is None
    assert (current.page_count, current.chunk_count, current.token_count) == (None, None, None)
    assert current.failure_code is None and not current.failure_retryable
    assert current.file_name is None
    assert current.content_type is None
    assert current.size_bytes is None
    assert current.blob_name is None
    assert blobs.delete_calls == [(SESSION, DOC)]
    assert blobs.control_delete_calls == [(SESSION, DOC)]
    assert search.delete_calls == [(SESSION, DOC)]

    persisted = repository.persisted_text_for_test()
    for sensitive in (
        "private-invoice.pdf",
        "application/pdf",
        f"uploads/{SESSION}/{DOC}",
        "Private invoice",
        "result-private",
        "private-operation",
        "private-failure",
        "private\":\"content",
    ):
        assert sensitive not in persisted


async def test_completed_deletion_clears_content_range() -> None:
    service, repository, _, _ = await make_service(
        record().model_copy(update={"content_range": "pages=2-4"})
    )
    await service.request_delete(SESSION, DOC, NOW)

    result = await service.sweep_pending(NOW, 10)

    current = (await repository.get(SESSION, DOC)).value  # type: ignore[union-attr]
    assert result.deleted == 1
    assert current.state is DocumentState.DELETED
    assert current.content_range is None
    assert "pages=2-4" not in repository.persisted_text_for_test()


@pytest.mark.parametrize("failing", ["blob", "search"])
async def test_transient_artifact_failure_stays_deleting(failing: str) -> None:
    service, repository, blobs, search = await make_service(record())
    await service.request_delete(SESSION, DOC, NOW)
    target = blobs if failing == "blob" else search
    target.error = TransientArtifactError("private document content")

    result = await service.sweep_pending(NOW, 10)

    assert (result.deleted, result.pending) == (0, 1)
    assert (await repository.get(SESSION, DOC)).value.state is DocumentState.DELETING  # type: ignore[union-attr]
    assert "private document content" not in repr(result)


async def test_control_blob_transient_failure_retries_without_accumulating_identifiers() -> None:
    service, _, blobs, _ = await make_service(record())
    await service.request_delete(SESSION, DOC, NOW)
    original = blobs.delete_control_blob
    attempts = 0

    async def transient_once(session_key: str, document_id: UUID) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransientArtifactError("sig=SECRET private document content")
        await original(session_key, document_id)

    blobs.delete_control_blob = transient_once  # type: ignore[method-assign]
    first = await service.sweep_pending(NOW, 10)
    second = await service.sweep_pending(NOW + timedelta(minutes=1), 10)

    assert first.pending == 1 and second.deleted == 1
    assert blobs.control_delete_calls == [(SESSION, DOC)]


async def test_missing_artifacts_and_repeated_sweeps_are_idempotent() -> None:
    service, repository, blobs, search = await make_service(record())
    await service.request_delete(SESSION, DOC, NOW)
    blobs.present = False
    search.present = False

    first = await service.sweep_pending(NOW, 10)
    second = await service.sweep_pending(NOW + timedelta(minutes=1), 10)

    assert (first.deleted, first.pending) == (1, 0)
    assert (second.deleted, second.pending) == (0, 0)
    assert (await repository.get(SESSION, DOC)).value.state is DocumentState.DELETED  # type: ignore[union-attr]


async def test_expired_live_document_is_tombstoned_then_deleted() -> None:
    service, repository, _, _ = await make_service(
        record(state=DocumentState.FAILED, expires_at=NOW)
    )

    result = await service.sweep_pending(NOW, 10)
    current = (await repository.get(SESSION, DOC)).value  # type: ignore[union-attr]

    assert result.deleted == 1
    assert current.state is DocumentState.DELETED
    assert current.tombstoned_at == NOW
    assert current.deletion_requested_at == NOW


async def test_sweep_scans_repeated_continuations_past_busy_page_to_empty_end() -> None:
    busy = [record().model_copy(update={"document_id": UUID(int=value)}) for value in range(1, 101)]
    deletable = record().model_copy(update={"document_id": UUID(int=101)})
    pages = {
        None: ([item.model_copy(update={"state": DocumentState.DELETING}) for item in busy], "next"),
        "next": ([deletable.model_copy(update={"state": DocumentState.DELETING})], "end"),
        "end": ([], None),
    }

    class PagedRepository(MemoryDocumentRepository):
        def __init__(self) -> None:
            super().__init__()
            self.cursors: list[str | None] = []

        async def list_lifecycle_candidates(self, now, limit, continuation=None):  # type: ignore[no-untyped-def,override]
            del now, limit
            self.cursors.append(continuation)
            values, following = pages[continuation]
            return [VersionedDocument(value=value, etag='W/"1"') for value in values], following

        async def get(self, session_key: str, document_id: UUID):  # type: ignore[no-untyped-def,override]
            del session_key
            if document_id.int <= 100:
                return VersionedDocument(value=busy[document_id.int - 1].model_copy(update={"state": DocumentState.DELETING}), etag='W/"1"')
            return await super().get(SESSION, document_id)

    class SelectiveBlobs(Blobs):
        @asynccontextmanager
        async def acquire_document_lease(self, session_key: str, document_id: UUID):  # type: ignore[no-untyped-def,override]
            if document_id.int <= 100:
                raise DocumentLeaseBusy
            yield self.lease

    repository = PagedRepository()
    await repository.create(deletable.model_copy(update={"state": DocumentState.DELETING}))
    service = DeletionService(repository, SelectiveBlobs(), Search())

    result = await service.sweep_pending(NOW, 102)

    assert result.pending == 100 and result.deleted == 1
    assert repository.cursors == [None, "next", "end"]


async def test_purge_uses_exact_48_hour_boundary_and_requires_absent_artifacts() -> None:
    deleted = record(
        state=DocumentState.DELETED,
        expires_at=NOW - timedelta(days=3),
        deleted_at=NOW - timedelta(hours=48),
    ).model_copy(update={"tombstoned_at": NOW - timedelta(hours=49)})
    service, repository, blobs, search = await make_service(deleted)

    assert await service.purge_deleted(NOW - timedelta(microseconds=1), 10) == 0
    assert await repository.get(SESSION, DOC) is not None
    assert await service.purge_deleted(NOW, 10) == 0
    blobs.present = False
    search.present = False
    blobs.control_delete_calls.append((SESSION, DOC))
    assert await service.purge_deleted(NOW, 10) == 1
    assert await repository.get(SESSION, DOC) is None


async def test_redelivery_guard_checks_before_and_after_lease() -> None:
    service, repository, _, _ = await make_service(record())
    lease = Lease()
    await service.guard_not_tombstoned(SESSION, DOC, lease)
    current = await repository.get(SESSION, DOC)
    assert current is not None
    await repository.replace(
        current.value.model_copy(
            update={"state": DocumentState.DELETING, "tombstoned_at": NOW}
        ),
        current.etag,
    )

    with pytest.raises(TombstonedDocument):
        await service.guard_not_tombstoned(SESSION, DOC, lease)
    assert lease.checks == 2


async def test_worker_write_lease_rechecks_after_acquisition_before_entering_body() -> None:
    service, repository, blobs, _ = await make_service(record())
    entered = False

    async def tombstone_during_acquisition() -> None:
        current = await repository.get(SESSION, DOC)
        assert current is not None
        await repository.replace(
            current.value.model_copy(
                update={"state": DocumentState.DELETING, "tombstoned_at": NOW}
            ),
            current.etag,
        )

    blobs.on_acquire = tombstone_during_acquisition
    with pytest.raises(TombstonedDocument):
        async with service.acquire_write_lease(SESSION, DOC):
            entered = True
    assert not entered


async def test_lease_loss_aborts_before_physical_delete() -> None:
    service, repository, blobs, search = await make_service(record())
    await service.request_delete(SESSION, DOC, NOW)
    blobs.lease.valid = False

    result = await service.sweep_pending(NOW, 10)

    assert result.pending == 1
    assert blobs.delete_calls == [] and search.delete_calls == []
    assert (await repository.get(SESSION, DOC)).value.state is DocumentState.DELETING  # type: ignore[union-attr]


async def test_cancelled_sweep_propagates_cancellation_and_keeps_tombstone() -> None:
    service, repository, blobs, _ = await make_service(record())
    await service.request_delete(SESSION, DOC, NOW)

    async def cancelled(session_key: str, document_id: UUID) -> None:
        del session_key, document_id
        raise asyncio.CancelledError

    blobs.delete_document_artifacts = cancelled  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await service.sweep_pending(NOW, 10)
    assert (await repository.get(SESSION, DOC)).value.state is DocumentState.DELETING  # type: ignore[union-attr]
