import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.repositories.memory_repository import MemoryApplicationRepository, MemoryDocumentRepository
from app.services.blob_service import UploadGrant, VerifiedUpload
from app.services.session_service import SessionService
from app.services.upload_service import UploadService

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
TOKEN = b"x" * 32
OTHER_TOKEN = b"y" * 32
SESSION_KEY = sha256(TOKEN).hexdigest()
DOCUMENT_ID = UUID("11111111-1111-4111-8111-111111111111")


class Clock:
    def now(self) -> datetime:
        return NOW


class Blobs:
    def __init__(self) -> None:
        self.closed = 0
        self.created: list[tuple[str, str]] = []

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
        return VerifiedUpload(header=b"%PDF-1.7", package=None)

    async def aclose(self) -> None:
        self.closed += 1


class Dispatcher:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def dispatch_outbox(self, outbox_id: str) -> bool:
        return bool(outbox_id)

    async def run(self) -> None:
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def make_client() -> tuple[TestClient, MemoryDocumentRepository]:
    settings = Settings(app_mode="test")
    repository = MemoryApplicationRepository()
    sessions = repository.sessions
    session_service = SessionService(
        sessions,
        Clock(),
        settings=settings,
        token_factory=iter((TOKEN, OTHER_TOKEN)).__next__,
        session_documents=repository,
    )
    documents = repository.documents
    dispatcher = Dispatcher()
    upload_service = UploadService(
        session_service,
        documents,
        Blobs(),
        dispatcher,
        Clock(),
        document_id_factory=lambda: DOCUMENT_ID,
    )
    app = create_app(
        settings=settings,
        session_service=session_service,
        upload_service=upload_service,
        outbox_dispatcher=dispatcher,
        enable_outbox_dispatcher=False,
    )
    return TestClient(app), documents


def test_init_rotates_cookie_and_returns_exact_safe_shape() -> None:
    client, documents = make_client()

    response = client.post(
        "/api/uploads/init",
        json={"fileName": "a.pdf", "contentType": "application/pdf", "sizeBytes": 8},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["set-cookie"].lower().startswith("cu_session=")
    assert response.json() == {
        "uploadUrl": (
            f"https://account.blob.core.windows.net/uploads/uploads/{SESSION_KEY}/"
            f"{DOCUMENT_ID}/a.pdf?sig=secret"
        ),
        "documentId": str(DOCUMENT_ID),
        "expiresAt": "2026-09-03T10:15:00Z",
        "requiredHeaders": {"x-ms-blob-type": "BlockBlob"},
    }
    assert not ({"sessionKey", "token", "hash", "blobName", "path"} & response.json().keys())
    stored = documents.persisted_text_for_test()
    assert "sig=secret" not in stored
    assert TOKEN.decode() not in stored


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
def test_init_rejects_non_basename_with_stable_nonretryable_error(file_name: str) -> None:
    client, documents = make_client()

    response = client.post(
        "/api/uploads/init",
        json={"fileName": file_name, "contentType": "application/pdf", "sizeBytes": 8},
    )

    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"] == {
        "code": "invalid_file_name",
        "message": "The file name is invalid.",
        "retryable": False,
        "correlationId": response.headers["x-correlation-id"],
    }
    assert documents.persisted_text_for_test() == ""


@pytest.mark.parametrize("control", ["\x00", "\n", "\u200b", "\u200d", "\ufeff"])
@pytest.mark.parametrize("file_name_template", ["{}a.pdf", "a{}b.pdf", "a{}.pdf"])
def test_init_rejects_control_characters_with_stable_error_before_side_effects(
    control: str, file_name_template: str
) -> None:
    client, documents = make_client()
    blobs = client.app.state.upload_service._blobs

    response = client.post(
        "/api/uploads/init",
        json={
            "fileName": file_name_template.format(control),
            "contentType": "application/pdf",
            "sizeBytes": 8,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_file_name"
    assert documents.persisted_text_for_test() == ""
    assert blobs.created == []


def test_complete_accepts_only_etag_and_returns_document_response() -> None:
    client, _ = make_client()
    initialized = client.post(
        "/api/uploads/init",
        json={"fileName": "a.pdf", "contentType": "application/pdf", "sizeBytes": 8},
    )
    assert initialized.status_code == 200

    response = client.post(
        f"/api/uploads/{DOCUMENT_ID}/complete", json={"etag": '"etag"'}
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["documentId"] == str(DOCUMENT_ID)
    assert response.json()["state"] == "queued"
    assert not ({"sessionKey", "blobName", "uploadUrl"} & response.json().keys())


@pytest.mark.parametrize("etag", ["", "x" * 257, '"bad space"', '"bad\nline"'])
def test_complete_rejects_invalid_etag(etag: str) -> None:
    client, _ = make_client()
    response = client.post(f"/api/uploads/{DOCUMENT_ID}/complete", json={"etag": etag})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_init_rejects_client_blob_path_and_unknown_fields() -> None:
    client, _ = make_client()
    response = client.post(
        "/api/uploads/init",
        json={
            "fileName": "a.pdf",
            "contentType": "application/pdf",
            "sizeBytes": 8,
            "blobName": "attacker/path",
        },
    )
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"error"}
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["retryable"] is False
    assert response.json()["error"]["correlationId"] == response.headers["x-correlation-id"]
    assert "blobName" not in response.text


def test_cross_session_complete_returns_404() -> None:
    owner, _ = make_client()
    initialized = owner.post(
        "/api/uploads/init",
        json={"fileName": "a.pdf", "contentType": "application/pdf", "sizeBytes": 8},
    )
    assert initialized.status_code == 200
    owner.cookies.set("cu_session", "eXl5eXl5eXl5eXl5eXl5eXl5eXl5eXl5eXl5eXl5eXk")

    response = owner.post(
        f"/api/uploads/{DOCUMENT_ID}/complete", json={"etag": '"etag"'}
    )
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "document_not_found"


async def _ready() -> bool:
    return True


def test_production_upload_cookie_has_all_security_attributes() -> None:
    settings = Settings(app_mode="production")
    repository = MemoryApplicationRepository()
    sessions = repository.sessions
    session_service = SessionService(
        sessions,
        Clock(),
        settings=settings,
        token_factory=lambda: TOKEN,
        session_documents=repository,
    )
    documents = repository.documents
    dispatcher = Dispatcher()
    uploads = UploadService(
        session_service,
        documents,
        Blobs(),
        dispatcher,
        Clock(),
        document_id_factory=lambda: DOCUMENT_ID,
    )
    app = create_app(
        settings=settings,
        readiness_checks={
            name: _ready for name in ("blob", "queue", "table", "search", "foundry")
        },
        session_service=session_service,
        upload_service=uploads,
        outbox_dispatcher=dispatcher,
        enable_outbox_dispatcher=False,
    )
    response = TestClient(app).post(
        "/api/uploads/init",
        json={"fileName": "a.pdf", "contentType": "application/pdf", "sizeBytes": 8},
    )
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert "max-age=86400" in cookie


def test_lifespan_starts_and_cleanly_cancels_injected_dispatcher() -> None:
    settings = Settings(app_mode="test")
    repository = MemoryApplicationRepository()
    sessions = repository.sessions
    session_service = SessionService(
        sessions,
        Clock(),
        settings=settings,
        token_factory=lambda: TOKEN,
        session_documents=repository,
    )
    documents = repository.documents
    dispatcher = Dispatcher()
    blobs = Blobs()
    uploads = UploadService(session_service, documents, blobs, dispatcher, Clock())
    app = create_app(
        settings=settings,
        session_service=session_service,
        upload_service=uploads,
        outbox_dispatcher=dispatcher,
        enable_outbox_dispatcher=True,
    )
    with TestClient(app):
        assert dispatcher.started.is_set()
        assert dispatcher.cancelled is False
    assert dispatcher.cancelled is True
    assert blobs.closed == 1


@pytest.mark.parametrize("injected", ["upload_service", "outbox_dispatcher"])
def test_factory_rejects_incoherent_partial_upload_dependency_injection(injected: str) -> None:
    settings = Settings(app_mode="test")
    repository = MemoryApplicationRepository()
    sessions = repository.sessions
    session_service = SessionService(
        sessions,
        Clock(),
        settings=settings,
        token_factory=lambda: TOKEN,
        session_documents=repository,
    )
    documents = repository.documents
    dispatcher = Dispatcher()
    uploads = UploadService(session_service, documents, Blobs(), dispatcher, Clock())
    kwargs = {injected: uploads if injected == "upload_service" else dispatcher}
    with pytest.raises(ValueError, match="upload_service and outbox_dispatcher"):
        create_app(settings=settings, session_service=session_service, **kwargs)  # type: ignore[arg-type]


def test_factory_shares_injected_memory_session_state_with_default_upload_graph() -> None:
    settings = Settings(app_mode="test")
    sessions = MemoryApplicationRepository().sessions
    session_service = SessionService(sessions, Clock(), settings=settings)

    app = create_app(settings=settings, session_service=session_service)
    assert app.state.session_service is session_service