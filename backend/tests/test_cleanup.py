from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from app.cleanup import CLEANUP_PAGE_SIZE, async_main, run_cleanup_once
from app.domain.models import DocumentRecord, DocumentState
from app.main import ApplicationDependencies
from app.repositories.memory_repository import MemoryApplicationRepository, MemoryWorkQueue

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
SESSION = "a" * 64


class Lease:
    def ensure_valid(self) -> None:
        return None


class Blobs:
    def __init__(self, *, busy: bool = False) -> None:
        self.busy = busy
        self.present = True

    def acquire_document_lease(self, session_key: str, document_id: UUID):  # type: ignore[no-untyped-def]
        from contextlib import asynccontextmanager

        from app.services.blob_service import DocumentLeaseBusy

        @asynccontextmanager
        async def lease():  # type: ignore[no-untyped-def]
            if self.busy:
                raise DocumentLeaseBusy
            yield Lease()
        return lease()

    async def delete_document_artifacts(self, session_key: str, document_id: UUID) -> None:
        self.present = False

    async def document_artifacts_exist(self, session_key: str, document_id: UUID) -> bool:
        return self.present

    async def aclose(self) -> None:
        return None


class Search:
    def __init__(self) -> None:
        self.present = True

    async def delete_for_document(self, session_key: str, document_id: UUID) -> None:
        self.present = False

    async def has_for_document(self, session_key: str, document_id: UUID) -> bool:
        return self.present


def record(document_id: UUID, state: DocumentState, *, deleted_at: datetime | None = None) -> DocumentRecord:
    deleted = state is DocumentState.DELETED
    return DocumentRecord(
        session_key=SESSION, document_id=document_id,
        file_name=None if deleted else "safe.pdf", content_type=None if deleted else "application/pdf",
        size_bytes=None if deleted else 1, blob_name=None if deleted else f"uploads/{document_id}",
        state=state, created_at=NOW - timedelta(days=3), updated_at=NOW - timedelta(days=2),
        expires_at=NOW + timedelta(hours=1), deleted_at=deleted_at,
    )


async def dependencies(*, busy: bool = False) -> tuple[ApplicationDependencies, MemoryApplicationRepository, Blobs, Search]:
    repository = MemoryApplicationRepository()
    blobs = Blobs(busy=busy)
    search = Search()
    bundle = ApplicationDependencies(repository, MemoryWorkQueue(), blobs, search, {})
    return bundle, repository, blobs, search


async def test_cleanup_once_finishes_accepted_deletion_and_leaves_busy_lease_pending() -> None:
    bundle, repository, _, _ = await dependencies()
    await repository.documents.create(record(UUID(int=1), DocumentState.DELETING))
    result = await run_cleanup_once(bundle, NOW)
    assert result.deleted == 1
    assert (await repository.documents.get(SESSION, UUID(int=1))).value.state is DocumentState.DELETED  # type: ignore[union-attr]

    busy_bundle, busy_repository, _, _ = await dependencies(busy=True)
    await busy_repository.documents.create(record(UUID(int=2), DocumentState.DELETING))
    pending = await run_cleanup_once(busy_bundle, NOW)
    assert pending.pending == 1
    assert (await busy_repository.documents.get(SESSION, UUID(int=2))).value.state is DocumentState.DELETING  # type: ignore[union-attr]


async def test_cleanup_once_purges_exact_retention_boundary() -> None:
    bundle, repository, blobs, search = await dependencies()
    blobs.present = False
    search.present = False
    await repository.documents.create(
        record(UUID(int=3), DocumentState.DELETED, deleted_at=NOW - timedelta(hours=48))
    )
    result = await run_cleanup_once(bundle, NOW)
    assert result.purged == 1
    assert await repository.documents.get(SESSION, UUID(int=3)) is None


async def test_cleanup_main_closes_dependencies_when_processing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = 0

    class Bundle:
        application_repository = object()
        blob_store = object()
        chunk_search = object()

        async def aclose(self) -> None:
            nonlocal closed
            closed += 1

    async def fail(bundle: Any, now: datetime, limit: int = CLEANUP_PAGE_SIZE):
        del bundle, now, limit
        raise RuntimeError("systemic")

    monkeypatch.setattr("app.cleanup.run_cleanup_once", fail)
    assert await async_main(dependency_factory=lambda settings: Bundle()) == 1  # type: ignore[arg-type]
    assert closed == 1