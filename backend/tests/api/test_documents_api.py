import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.models import DocumentRecord, DocumentState, SessionRecord
from app.main import create_app
from app.repositories.memory_repository import MemoryApplicationRepository
from app.services.deletion_service import DeletionService
from app.services.document_service import DocumentService
from app.services.outbox_service import OutboxDispatcher
from app.services.session_service import SessionService

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
TOKEN = b"x" * 32
OTHER_TOKEN = b"y" * 32
SESSION = sha256(TOKEN).hexdigest()
OTHER_SESSION = sha256(OTHER_TOKEN).hexdigest()
DOC = UUID("11111111-1111-4111-8111-111111111111")
VALID_ORIGIN = {"Origin": "http://testserver"}


class Clock:
    def now(self) -> datetime:
        return NOW


class Queue:
    def __init__(self) -> None:
        self.messages = []

    async def enqueue_ingestion(self, message):  # type: ignore[no-untyped-def]
        self.messages.append(message)

    async def enqueue_result_cleanup(self, message):  # type: ignore[no-untyped-def]
        del message

    async def get_ingestion_backlog(self) -> int:
        return len(self.messages)


class DeleteBlobs:
    class Lease:
        def ensure_valid(self) -> None:
            return None

    class Context:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return DeleteBlobs.Lease()

        async def __aexit__(self, exc_type, exc, traceback):  # type: ignore[no-untyped-def]
            del exc_type, exc, traceback

    def acquire_document_lease(self, session_key: str, document_id: UUID):  # type: ignore[no-untyped-def]
        del session_key, document_id
        return self.Context()

    async def delete_document_artifacts(self, session_key: str, document_id: UUID) -> None:
        del session_key, document_id

    async def document_artifacts_exist(self, session_key: str, document_id: UUID) -> bool:
        del session_key, document_id
        return False


class Search:
    async def delete_for_document(self, session_key: str, document_id: UUID) -> None:
        del session_key, document_id

    async def has_for_document(self, session_key: str, document_id: UUID) -> bool:
        del session_key, document_id
        return False


class Uploads:
    closed = 0

    def __init__(self, documents, blobs: DeleteBlobs) -> None:  # type: ignore[no-untyped-def]
        self.documents = documents
        self.blobs = blobs

    async def initialize(self, session_key, request):  # type: ignore[no-untyped-def]
        del session_key, request
        raise AssertionError("not used")

    async def complete(self, session_key, document_id, expected_etag, correlation_id):  # type: ignore[no-untyped-def]
        del session_key, document_id, expected_etag, correlation_id
        raise AssertionError("not used")

    async def aclose(self) -> None:
        self.closed += 1


def record(
    document_id: UUID = DOC,
    *,
    session: str = SESSION,
    state: DocumentState = DocumentState.READY,
    created_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(hours=1),
    retryable: bool = False,
    retry_count: int = 0,
) -> DocumentRecord:
    deleted = state is DocumentState.DELETED
    return DocumentRecord(
        session_key=session,
        document_id=document_id,
        file_name=None if deleted else "private-invoice.pdf",
        content_type=None if deleted else "application/pdf",
        size_bytes=None if deleted else 100,
        blob_name=None if deleted else f"uploads/{session}/{document_id}/private-invoice.pdf",
        state=state,
        created_at=created_at,
        updated_at=created_at,
        expires_at=expires_at,
        document_type=None if deleted else "invoice",
        title=None if deleted else "Private invoice",
        content_result_id=None if deleted else "remote-result-secret",
        content_operation_url=None if deleted else "https://internal.example/operations/secret",
        extraction=None if deleted else {"vendorName": "Contoso", "total": 42},
        markdown_blob_name=None if deleted else f"derived/{session}/{document_id}/content.md",
        page_count=None if deleted else 2,
        chunk_count=None if deleted else 3,
        token_count=None if deleted else 50,
        failure_code=(
            "transient_failure" if state is DocumentState.FAILED and not deleted else None
        ),
        failure_retryable=retryable and not deleted,
        retry_count=retry_count,
    )


async def make_services():  # type: ignore[no-untyped-def]
    repository = MemoryApplicationRepository()
    settings = Settings(app_mode="test")
    sessions = SessionService(
        repository.sessions,
        Clock(),
        settings=settings,
        token_factory=iter((TOKEN, OTHER_TOKEN, b"z" * 32)).__next__,
        session_documents=repository,
    )
    await repository.sessions.create(
        SessionRecord(session_key=SESSION, created_at=NOW, expires_at=NOW + timedelta(hours=1))
    )
    await repository.sessions.create(
        SessionRecord(
            session_key=OTHER_SESSION,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
    )
    queue = Queue()
    dispatcher = OutboxDispatcher(repository.documents, queue, Clock())
    deletion = DeletionService(repository.documents, DeleteBlobs(), Search())  # type: ignore[arg-type]
    documents = DocumentService(repository.documents, deletion, dispatcher, Clock())
    return settings, repository, sessions, queue, dispatcher, deletion, documents


def make_client():  # type: ignore[no-untyped-def]
    settings, repository, sessions, queue, dispatcher, deletion, documents = asyncio.run(
        make_services()
    )
    uploads = Uploads(repository.documents, deletion._blobs)
    app = create_app(
        settings=settings,
        session_service=sessions,
        upload_service=uploads,  # type: ignore[arg-type]
        outbox_dispatcher=dispatcher,
        document_service=documents,
        deletion_service=deletion,
        enable_outbox_dispatcher=False,
    )
    client = TestClient(app)
    client.cookies.set("cu_session", "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg")
    return client, repository, queue, documents, uploads


def persist(repository: MemoryApplicationRepository, *documents: DocumentRecord) -> None:
    async def add() -> None:
        for document in documents:
            await repository.documents.create(document)

    asyncio.run(add())


def test_list_is_owner_scoped_hides_lifecycle_rows_and_sorts_newest_deterministically() -> None:
    client, repository, _, _, _ = make_client()
    older = record(UUID(int=2), created_at=NOW - timedelta(minutes=1))
    same_newer_a = record(UUID(int=3))
    same_newer_b = record(UUID(int=4))
    persist(
        repository,
        older,
        same_newer_b,
        same_newer_a,
        record(UUID(int=5), session=OTHER_SESSION),
        record(UUID(int=6), state=DocumentState.DELETING),
        record(UUID(int=7), state=DocumentState.DELETED),
    )

    response = client.get("/api/documents")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert [item["documentId"] for item in response.json()] == [
        str(same_newer_a.document_id),
        str(same_newer_b.document_id),
        str(older.document_id),
    ]
    assert set(response.json()[0]) == {
        "documentId",
        "fileName",
        "state",
        "documentType",
        "title",
        "pageCount",
        "chunkCount",
        "tokenCount",
        "failureCode",
        "failureRetryable",
        "retryCount",
        "createdAt",
        "updatedAt",
        "expiresAt",
    }
    assert "Contoso" not in response.text
    assert not (
        {"sessionKey", "blobName", "contentResultId", "contentOperationUrl"}
        & response.json()[0].keys()
    )


def test_get_returns_exact_owner_extraction_without_internal_fields_or_secrets() -> None:
    client, repository, _, _, _ = make_client()
    persist(repository, record())

    response = client.get(f"/api/documents/{DOC}")

    assert response.status_code == 200
    assert response.json()["extraction"] == {"vendorName": "Contoso", "total": 42}
    assert response.json()["retryCount"] == 0
    assert "remote-result-secret" not in response.text
    assert "internal.example" not in response.text
    assert not ({"sessionKey", "blobName", "markdownBlobName"} & response.json().keys())


@pytest.mark.parametrize("path", ["/api/documents", f"/api/documents/{DOC}"])
def test_get_with_missing_cookie_rotates_cookie_and_never_requires_origin(path: str) -> None:
    client, _, _, _, _ = make_client()
    client.cookies.clear()

    response = client.get(path)

    assert response.status_code in {200, 404}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["set-cookie"].lower().startswith("cu_session=")


@pytest.mark.parametrize("state", [DocumentState.DELETING, DocumentState.DELETED])
def test_get_hides_tombstoned_documents(state: DocumentState) -> None:
    client, repository, _, _, _ = make_client()
    persist(repository, record(state=state))
    assert client.get(f"/api/documents/{DOC}").status_code == 404


def test_get_cross_session_is_indistinguishable_from_unknown() -> None:
    client, repository, _, _, _ = make_client()
    persist(repository, record(session=OTHER_SESSION))
    response = client.get(f"/api/documents/{DOC}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "document_not_found"


async def test_retry_atomically_increments_and_concurrent_duplicates_create_one_outbox() -> None:
    _, repository, _, queue, dispatcher, _, documents = await make_services()
    await repository.documents.create(
        record(state=DocumentState.FAILED, retryable=True, retry_count=2)
    )
    calls: list[str] = []
    original = dispatcher.dispatch_outbox

    async def tracked(outbox_id: str) -> bool:
        calls.append(outbox_id)
        return await original(outbox_id)

    dispatcher.dispatch_outbox = tracked  # type: ignore[method-assign]

    first, second = await asyncio.gather(
        documents.retry(SESSION, DOC, UUID(int=1)),
        documents.retry(SESSION, DOC, UUID(int=2)),
    )

    assert first.state is second.state is DocumentState.QUEUED
    assert first.retry_count == second.retry_count == 3
    rows = await repository.documents.all_outbox_for_test()
    assert [row.outbox_id for row in rows] == [f"ingest:{DOC}:3"]
    assert rows[0].payload.correlation_id in {UUID(int=1), UUID(int=2)}  # type: ignore[union-attr]
    assert len(queue.messages) == 1
    assert calls == [f"ingest:{DOC}:3", f"ingest:{DOC}:3"]
    stored = await repository.documents.get(SESSION, DOC)
    assert stored is not None
    assert stored.value.failure_code is None
    assert stored.value.failure_retryable is False


async def test_retry_returns_queued_while_failed_dispatch_remains_durably_pending() -> None:
    _, repository, _, _, _, _, documents = await make_services()
    await repository.documents.create(record(state=DocumentState.FAILED, retryable=True))

    async def fail_dispatch(outbox_id: str) -> bool:
        del outbox_id
        raise RuntimeError("private queue failure")

    documents._dispatcher.dispatch_outbox = fail_dispatch  # type: ignore[method-assign]
    response = await documents.retry(SESSION, DOC, UUID(int=1))

    assert response.state is DocumentState.QUEUED
    pending = await repository.documents.get_pending_outbox(f"ingest:{DOC}:1")
    assert pending is not None


@pytest.mark.parametrize(
    ("state", "retryable", "expires_at"),
    [
        (DocumentState.READY, True, NOW + timedelta(hours=1)),
        (DocumentState.FAILED, False, NOW + timedelta(hours=1)),
        (DocumentState.FAILED, True, NOW),
        (DocumentState.DELETING, True, NOW + timedelta(hours=1)),
        (DocumentState.DELETED, True, NOW + timedelta(hours=1)),
    ],
)
def test_retry_matrix_rejects_invalid_state_retryability_expiry_and_tombstone(
    state: DocumentState, retryable: bool, expires_at: datetime
) -> None:
    client, repository, _, _, _ = make_client()
    persist(repository, record(state=state, retryable=retryable, expires_at=expires_at))

    response = client.post(f"/api/documents/{DOC}/retry", headers=VALID_ORIGIN)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_document_state"


def test_retry_cross_session_is_404() -> None:
    client, repository, _, _, _ = make_client()
    persist(repository, record(session=OTHER_SESSION, state=DocumentState.FAILED, retryable=True))
    assert client.post(f"/api/documents/{DOC}/retry", headers=VALID_ORIGIN).status_code == 404


def test_retry_success_returns_queued_typed_state_and_dispatches_target_outbox() -> None:
    client, repository, queue, _, _ = make_client()
    persist(repository, record(state=DocumentState.FAILED, retryable=True, retry_count=1))

    response = client.post(f"/api/documents/{DOC}/retry", headers=VALID_ORIGIN)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["state"] == "queued"
    assert response.json()["retryCount"] == 2
    assert response.json()["failureCode"] is None
    assert response.json()["failureRetryable"] is False
    assert len(queue.messages) == 1
    assert queue.messages[0].document_id == DOC


def test_delete_returns_202_tombstone_and_is_idempotent_then_hidden() -> None:
    client, repository, _, _, _ = make_client()
    persist(repository, record())

    first = client.delete(f"/api/documents/{DOC}", headers=VALID_ORIGIN)
    second = client.delete(f"/api/documents/{DOC}", headers=VALID_ORIGIN)

    assert first.status_code == second.status_code == 202
    assert first.json()["state"] == second.json()["state"] == "deleting"
    assert first.json()["documentId"] == str(DOC)
    assert "extraction" not in first.json()
    assert client.get(f"/api/documents/{DOC}").status_code == 404
    assert client.get("/api/documents").json() == []


def test_delete_cross_session_and_unknown_are_404() -> None:
    client, repository, _, _, _ = make_client()
    persist(repository, record(session=OTHER_SESSION))
    assert client.delete(f"/api/documents/{DOC}", headers=VALID_ORIGIN).status_code == 404
    assert client.delete(f"/api/documents/{UUID(int=9)}", headers=VALID_ORIGIN).status_code == 404


@pytest.mark.parametrize("method", ["post", "delete"])
def test_mutation_origin_guard_runs_before_cookie_or_service_side_effects(method: str) -> None:
    client, repository, _, documents, _ = make_client()
    persist(repository, record(state=DocumentState.FAILED, retryable=True))
    client.cookies.clear()
    retry_calls = documents.retry_calls
    delete_calls = documents.delete_calls
    path = f"/api/documents/{DOC}/retry" if method == "post" else f"/api/documents/{DOC}"

    response = getattr(client, method)(path, headers={"Origin": "https://evil.example"})

    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert documents.retry_calls == retry_calls
    assert documents.delete_calls == delete_calls
    assert response.headers["cache-control"] == "no-store"


def test_factory_default_document_graph_shares_local_repository_and_closes_only_owned_uploads() -> None:
    app = create_app(settings=Settings(app_mode="test"), enable_outbox_dispatcher=False)
    assert app.state.document_service.repository is app.state.document_repository
    assert app.state.deletion_service._repository is app.state.document_repository

    client, _, _, _, uploads = make_client()
    with client:
        pass
    assert uploads.closed == 0
