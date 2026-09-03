import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest

from app.core.errors import AppError, ConcurrencyConflict
from app.domain.models import (
    DocumentRecord,
    DocumentState,
    IngestionMessage,
    OutboxRecord,
    UploadInitRequest,
)
from app.repositories.memory_repository import (
    MemoryApplicationRepository,
    MemoryDocumentRepository,
    MemoryWorkQueue,
)
from app.services.blob_service import UploadGrant, VerifiedUpload
from app.services.outbox_service import OutboxDispatcher
from app.services.session_service import SessionService
from app.services.upload_service import UploadService

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
TOKEN = b"x" * 32
SESSION_KEY = sha256(TOKEN).hexdigest()
OTHER_KEY = sha256(b"y" * 32).hexdigest()
DOCUMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
CORRELATION_ID = UUID("22222222-2222-4222-8222-222222222222")


class Clock:
    def now(self) -> datetime:
        return NOW


class Blobs:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.verified: list[tuple[str, str, int, str, bool]] = []
        self.content = b"%PDF-1.7"

    async def create_upload(self, blob_name: str, content_type: str) -> UploadGrant:
        self.created.append((blob_name, content_type))
        return UploadGrant(
            upload_url=f"https://account.blob.core.windows.net/uploads/{blob_name}?sig=secret",
            expires_at=NOW + timedelta(minutes=15),
            required_headers={"x-ms-blob-type": "BlockBlob"},
        )

    async def verify_upload(
        self,
        blob_name: str,
        expected_etag: str,
        expected_size: int,
        expected_content_type: str,
        *,
        office: bool,
    ) -> VerifiedUpload:
        self.verified.append(
            (blob_name, expected_etag, expected_size, expected_content_type, office)
        )
        return VerifiedUpload(header=self.content[:16], package=self.content if office else None)


class BrokenGrantBlobs(Blobs):
    async def create_upload(self, blob_name: str, content_type: str) -> UploadGrant:
        raise RuntimeError("delegation key unavailable")


class Dispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch_outbox(self, outbox_id: str) -> bool:
        self.calls.append(outbox_id)
        return True


class FailingTransactionRepository(MemoryApplicationRepository):
    async def reserve_and_create(
        self, session_update: object, session_etag: str, document: DocumentRecord
    ):  # type: ignore[no-untyped-def]
        del session_update, session_etag, document
        raise RuntimeError("transaction failed before commit")


class ConflictOnceRepository(MemoryApplicationRepository):
    def __init__(self) -> None:
        super().__init__()
        self.conflicts = 0

    async def reserve_and_create(
        self, session_update: object, session_etag: str, document: DocumentRecord
    ):  # type: ignore[no-untyped-def]
        if self.conflicts == 0:
            self.conflicts += 1
            raise ConcurrencyConflict
        return await super().reserve_and_create(session_update, session_etag, document)  # type: ignore[arg-type]


class BrokenDispatcher(Dispatcher):
    async def dispatch_outbox(self, outbox_id: str) -> bool:
        self.calls.append(outbox_id)
        raise RuntimeError("outbox listing unavailable")


class TransitionOnCommitRepository(MemoryDocumentRepository):
    def __init__(self, application: MemoryApplicationRepository, state: DocumentState) -> None:
        super().__init__(application._state)
        self.state = state

    async def commit_queued_with_outbox(
        self, document: DocumentRecord, document_etag: str, outbox: object
    ):  # type: ignore[no-untyped-def]
        del document_etag, outbox
        current = await self.get(document.session_key, document.document_id)
        assert current is not None
        await self.replace(current.value.model_copy(update={"state": self.state}), current.etag)
        raise ConcurrencyConflict


async def setup(
    documents: MemoryApplicationRepository | None = None,
    blobs: Blobs | None = None,
    document_repository: MemoryDocumentRepository | None = None,
) -> tuple[UploadService, MemoryDocumentRepository, object, Blobs, Dispatcher]:
    repository = documents or MemoryApplicationRepository()
    session_service = SessionService(
        repository.sessions,
        Clock(),
        token_factory=lambda: TOKEN,
        session_documents=repository,
    )
    await session_service.issue()
    actual_blobs = blobs or Blobs()
    dispatcher = Dispatcher()
    service = UploadService(
        session_service,
        document_repository or repository.documents,
        actual_blobs,
        dispatcher,
        Clock(),
        document_id_factory=lambda: DOCUMENT_ID,
    )
    return (
        service,
        document_repository or repository.documents,
        repository.sessions,
        actual_blobs,
        dispatcher,
    )


async def test_init_reserves_quota_creates_record_and_uses_server_path() -> None:
    service, documents, sessions, blobs, _ = await setup()

    response = await service.initialize(
        SESSION_KEY,
        UploadInitRequest(file_name="safe name.pdf", content_type="application/pdf", size_bytes=8),
    )

    assert response.document_id == DOCUMENT_ID
    assert response.required_headers == {"x-ms-blob-type": "BlockBlob"}
    blob_name = f"uploads/{SESSION_KEY}/{DOCUMENT_ID}/safe name.pdf"
    assert blobs.created == [(blob_name, "application/pdf")]
    stored = await documents.get(SESSION_KEY, DOCUMENT_ID)
    assert stored is not None
    assert stored.value.state == DocumentState.AWAITING_UPLOAD
    assert stored.value.blob_name == blob_name
    assert "sig=secret" not in stored.value.model_dump_json()
    quota = await sessions.get(SESSION_KEY)
    assert quota is not None
    assert (quota[0].document_count, quota[0].total_bytes) == (1, 8)


@pytest.mark.parametrize(
    "file_name",
    [
        "../../invoice.pdf",
        "..\\..\\invoice.pdf",
        "directory/file.pdf",
        "/absolute/invoice.pdf",
        "C:\\absolute\\invoice.pdf",
        "\\\\server\\share\\invoice.pdf",
        "directory\\nested/invoice.pdf",
    ],
)
async def test_init_rejects_path_components_before_any_side_effect(file_name: str) -> None:
    service, documents, sessions, blobs, _ = await setup()

    with pytest.raises(AppError) as caught:
        await service.initialize(
            SESSION_KEY,
            UploadInitRequest(
                file_name=file_name,
                content_type="application/pdf",
                size_bytes=8,
            ),
        )

    assert caught.value.code == "invalid_file_name"
    assert caught.value.status_code == 400
    assert caught.value.retryable is False
    quota = await sessions.get(SESSION_KEY)
    assert quota is not None
    assert (quota[0].document_count, quota[0].total_bytes) == (0, 0)
    assert await documents.get(SESSION_KEY, DOCUMENT_ID) is None
    assert blobs.created == []


@pytest.mark.parametrize("control", ["\x00", "\n", "\u200b", "\u200d", "\ufeff"])
@pytest.mark.parametrize("file_name_template", ["{}a.pdf", "a{}b.pdf", "a{}.pdf"])
async def test_init_rejects_control_characters_before_any_side_effect(
    control: str, file_name_template: str
) -> None:
    service, documents, sessions, blobs, _ = await setup()

    with pytest.raises(AppError) as caught:
        await service.initialize(
            SESSION_KEY,
            UploadInitRequest(
                file_name=file_name_template.format(control),
                content_type="application/pdf",
                size_bytes=8,
            ),
        )

    assert caught.value.code == "invalid_file_name"
    quota = await sessions.get(SESSION_KEY)
    assert quota is not None
    assert (quota[0].document_count, quota[0].total_bytes) == (0, 0)
    assert await documents.get(SESSION_KEY, DOCUMENT_ID) is None
    assert blobs.created == []


async def test_init_normalizes_valid_decomposed_unicode_before_side_effects() -> None:
    service, documents, _, blobs, _ = await setup()

    await service.initialize(
        SESSION_KEY,
        UploadInitRequest(file_name="Cafe\u0301.pdf", content_type="application/pdf", size_bytes=8),
    )

    normalized_name = "Caf\u00e9.pdf"
    blob_name = f"uploads/{SESSION_KEY}/{DOCUMENT_ID}/{normalized_name}"
    assert blobs.created == [(blob_name, "application/pdf")]
    stored = await documents.get(SESSION_KEY, DOCUMENT_ID)
    assert stored is not None and stored.value.file_name == normalized_name


async def test_init_transaction_failure_leaves_quota_and_documents_unchanged() -> None:
    service, documents, sessions, _, _ = await setup(FailingTransactionRepository())

    with pytest.raises(AppError) as caught:
        await service.initialize(
            SESSION_KEY,
            UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=8),
        )
    assert caught.value.code == "document_create_failed"
    quota = await sessions.get(SESSION_KEY)
    assert quota is not None
    assert (quota[0].document_count, quota[0].total_bytes) == (0, 0)
    assert await documents.list_for_session(SESSION_KEY) == []


async def test_init_sas_failure_needs_no_compensation_and_leaves_state_unchanged() -> None:
    service, documents, sessions, _, _ = await setup(blobs=BrokenGrantBlobs())

    with pytest.raises(AppError) as caught:
        await service.initialize(
            SESSION_KEY,
            UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=8),
        )
    assert caught.value.code == "upload_grant_failed"
    assert await documents.get(SESSION_KEY, DOCUMENT_ID) is None
    quota = await sessions.get(SESSION_KEY)
    assert quota is not None
    assert (quota[0].document_count, quota[0].total_bytes) == (0, 0)


async def test_init_conflict_retries_transaction_without_persisting_duplicate_document() -> None:
    repository = ConflictOnceRepository()
    service, documents, sessions, blobs, _ = await setup(repository)

    response = await service.initialize(
        SESSION_KEY,
        UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=8),
    )

    assert response.document_id == DOCUMENT_ID
    assert repository.conflicts == 1
    assert len(blobs.created) == 1
    assert len(await documents.list_for_session(SESSION_KEY)) == 1
    quota = await sessions.get(SESSION_KEY)
    assert quota is not None
    assert (quota[0].document_count, quota[0].total_bytes) == (1, 8)


async def test_init_enforces_session_quota() -> None:
    service, _, _, _, _ = await setup()
    for index in range(5):
        service._document_id_factory = lambda index=index: UUID(int=index + 1)
        await service.initialize(
            SESSION_KEY,
            UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=1),
        )
    with pytest.raises(AppError) as caught:
        await service.initialize(
            SESSION_KEY,
            UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=1),
        )
    assert caught.value.code == "document_quota_exceeded"


async def test_complete_verifies_blob_and_atomically_queues_exact_outbox() -> None:
    service, documents, _, blobs, dispatcher = await setup()
    await service.initialize(
        SESSION_KEY, UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=8)
    )

    response = await service.complete(SESSION_KEY, DOCUMENT_ID, '"etag"', CORRELATION_ID)

    assert response.state == DocumentState.QUEUED
    assert blobs.verified == [
        (f"uploads/{SESSION_KEY}/{DOCUMENT_ID}/a.pdf", '"etag"', 8, "application/pdf", False)
    ]
    stored = await documents.get(SESSION_KEY, DOCUMENT_ID)
    assert stored is not None and stored.value.state == DocumentState.QUEUED
    outbox_rows = await documents.all_outbox_for_test()
    assert len(outbox_rows) == 1
    outbox = outbox_rows[0]
    assert outbox.outbox_id == f"ingest:{DOCUMENT_ID}:1"
    assert json.loads(outbox.payload.model_dump_json()) == {
        "version": 1,
        "sessionKey": SESSION_KEY,
        "documentId": str(DOCUMENT_ID),
        "blobName": f"uploads/{SESSION_KEY}/{DOCUMENT_ID}/a.pdf",
        "correlationId": str(CORRELATION_ID),
        "enqueuedAt": "2026-09-03T10:00:00Z",
        "resumeStage": "analyzing",
    }
    assert dispatcher.calls == [f"ingest:{DOCUMENT_ID}:1"]


async def test_complete_succeeds_and_safely_logs_when_opportunistic_dispatch_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = MemoryApplicationRepository()
    sessions = repository.sessions
    session_service = SessionService(
        sessions, Clock(), token_factory=lambda: TOKEN, session_documents=repository
    )
    await session_service.issue()
    documents = repository.documents
    service = UploadService(
        session_service,
        documents,
        Blobs(),
        BrokenDispatcher(),
        Clock(),
        document_id_factory=lambda: DOCUMENT_ID,
    )
    await service.initialize(
        SESSION_KEY, UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=8)
    )

    with caplog.at_level("WARNING"):
        response = await service.complete(SESSION_KEY, DOCUMENT_ID, '"etag"', CORRELATION_ID)

    assert response.state == DocumentState.QUEUED
    pending = await documents.list_pending_outbox(10)
    assert len(pending) == 1
    assert "opportunistic_outbox_dispatch_failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert SESSION_KEY not in caplog.text
    assert "sig=secret" not in caplog.text


async def test_repeated_complete_is_idempotent_and_redispatches_without_new_outbox() -> None:
    service, documents, _, _, dispatcher = await setup()
    await service.initialize(
        SESSION_KEY, UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=8)
    )
    first = await service.complete(SESSION_KEY, DOCUMENT_ID, '"etag"', CORRELATION_ID)
    second = await service.complete(SESSION_KEY, DOCUMENT_ID, '"different"', CORRELATION_ID)

    assert second == first
    assert len(await documents.all_outbox_for_test()) == 1
    assert dispatcher.calls == [f"ingest:{DOCUMENT_ID}:1"] * 2


@pytest.mark.parametrize(
    "state",
    [
        DocumentState.QUEUED,
        DocumentState.ANALYZING,
        DocumentState.CLASSIFIED,
        DocumentState.EXTRACTED,
        DocumentState.RESULT_CLEANUP_PENDING,
        DocumentState.CHUNKING,
        DocumentState.EMBEDDING,
        DocumentState.INDEXING,
        DocumentState.READY,
    ],
)
async def test_repeated_complete_returns_existing_valid_state_and_targets_its_outbox(
    state: DocumentState,
) -> None:
    service, documents, _, _, dispatcher = await setup()
    await service.initialize(
        SESSION_KEY, UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=8)
    )
    await service.complete(SESSION_KEY, DOCUMENT_ID, '"etag"', CORRELATION_ID)
    current = await documents.get(SESSION_KEY, DOCUMENT_ID)
    assert current is not None
    await documents.replace(current.value.model_copy(update={"state": state}), current.etag)
    dispatcher.calls.clear()

    response = await service.complete(SESSION_KEY, DOCUMENT_ID, '"ignored"', CORRELATION_ID)

    assert response.state == state
    assert dispatcher.calls == [f"ingest:{DOCUMENT_ID}:1"]


@pytest.mark.parametrize(
    "state", [DocumentState.FAILED, DocumentState.DELETING, DocumentState.DELETED]
)
async def test_complete_rejects_terminal_or_deleting_states_without_dispatch(
    state: DocumentState,
) -> None:
    service, documents, _, _, dispatcher = await setup()
    await service.initialize(
        SESSION_KEY, UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=8)
    )
    current = await documents.get(SESSION_KEY, DOCUMENT_ID)
    assert current is not None
    await documents.replace(current.value.model_copy(update={"state": state}), current.etag)

    with pytest.raises(AppError) as caught:
        await service.complete(SESSION_KEY, DOCUMENT_ID, '"ignored"', CORRELATION_ID)

    assert caught.value.code == "invalid_document_state"
    assert caught.value.status_code == 409
    assert dispatcher.calls == []


async def test_complete_never_dispatches_another_documents_pending_outbox() -> None:
    repository = MemoryApplicationRepository()
    session_service = SessionService(
        repository.sessions,
        Clock(),
        token_factory=lambda: TOKEN,
        session_documents=repository,
    )
    await session_service.issue()
    queue = MemoryWorkQueue()
    dispatcher = OutboxDispatcher(repository.documents, queue, Clock())
    service = UploadService(
        session_service,
        repository.documents,
        Blobs(),
        dispatcher,
        Clock(),
        document_id_factory=lambda: DOCUMENT_ID,
    )
    other_id = UUID("33333333-3333-4333-8333-333333333333")
    other_message = IngestionMessage(
        version=1,
        session_key=SESSION_KEY,
        document_id=other_id,
        blob_name=f"uploads/{SESSION_KEY}/{other_id}/b.pdf",
        correlation_id=CORRELATION_ID,
        enqueued_at=NOW,
    )
    await repository.documents.put_outbox_for_test(
        OutboxRecord(
            outbox_id=f"ingest:{other_id}:1",
            session_key=SESSION_KEY,
            kind="ingestion",
            payload=other_message,
            created_at=NOW,
        )
    )
    await service.initialize(
        SESSION_KEY, UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=8)
    )

    await service.complete(SESSION_KEY, DOCUMENT_ID, '"etag"', CORRELATION_ID)
    await service.complete(SESSION_KEY, DOCUMENT_ID, '"ignored"', CORRELATION_ID)

    assert [message.document_id for message in queue.ingestion_messages] == [DOCUMENT_ID]
    pending = await repository.documents.list_pending_outbox(10)
    assert [record.outbox_id for record, _ in pending] == [f"ingest:{other_id}:1"]


async def test_cross_session_complete_is_not_found_without_blob_access() -> None:
    service, _, _, blobs, _ = await setup()
    await service.initialize(
        SESSION_KEY, UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=8)
    )
    with pytest.raises(AppError) as caught:
        await service.complete(OTHER_KEY, DOCUMENT_ID, '"etag"', CORRELATION_ID)
    assert caught.value.code == "document_not_found"
    assert caught.value.status_code == 404
    assert blobs.verified == []


async def test_invalid_state_cannot_be_completed() -> None:
    service, documents, _, _, _ = await setup()
    await service.initialize(
        SESSION_KEY, UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=8)
    )
    stored = await documents.get(SESSION_KEY, DOCUMENT_ID)
    assert stored is not None
    await documents.replace(stored.value.model_copy(update={"state": DocumentState.FAILED}), stored.etag)
    with pytest.raises(AppError) as caught:
        await service.complete(SESSION_KEY, DOCUMENT_ID, '"etag"', CORRELATION_ID)
    assert caught.value.code == "invalid_document_state"


@pytest.mark.parametrize("state", [DocumentState.FAILED, DocumentState.DELETING])
async def test_concurrent_nonqueued_transition_is_not_reported_as_completed(
    state: DocumentState,
) -> None:
    repository = MemoryApplicationRepository()
    documents = TransitionOnCommitRepository(repository, state)
    service, _, _, _, _ = await setup(
        documents=repository, document_repository=documents
    )
    await service.initialize(
        SESSION_KEY, UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=8)
    )
    with pytest.raises(AppError) as caught:
        await service.complete(SESSION_KEY, DOCUMENT_ID, '"etag"', CORRELATION_ID)
    assert caught.value.code == "invalid_document_state"
    assert await documents.all_outbox_for_test() == []


async def test_complete_rejects_signature_before_outbox_commit() -> None:
    service, documents, _, blobs, _ = await setup()
    blobs.content = b"not-pdf"
    await service.initialize(
        SESSION_KEY,
        UploadInitRequest(file_name="a.pdf", content_type="application/pdf", size_bytes=len(blobs.content)),
    )
    with pytest.raises(AppError) as caught:
        await service.complete(SESSION_KEY, DOCUMENT_ID, '"etag"', CORRELATION_ID)
    assert caught.value.code == "invalid_file_content"
    stored = await documents.get(SESSION_KEY, DOCUMENT_ID)
    assert stored is not None and stored.value.state == DocumentState.AWAITING_UPLOAD
    assert await documents.all_outbox_for_test() == []