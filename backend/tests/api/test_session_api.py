from datetime import UTC, datetime, timedelta
from hashlib import sha256

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.repositories.memory_repository import MemorySessionRepository
from app.services.session_service import SessionService

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
TOKEN = b"x" * 32
SESSION_KEY = sha256(TOKEN).hexdigest()


class MutableClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current


def make_client(*, app_mode: str = "test") -> tuple[TestClient, MemorySessionRepository, MutableClock]:
    repository = MemorySessionRepository()
    clock = MutableClock()
    settings = Settings.model_validate({"app_mode": app_mode})
    tokens = iter((TOKEN, b"y" * 32, b"z" * 32))
    service = SessionService(
        repository, clock, settings=settings, token_factory=lambda: next(tokens)
    )
    return TestClient(create_app(settings=settings, session_service=service)), repository, clock


def test_first_call_sets_cookie_and_returns_only_public_quota_dto() -> None:
    client, repository, _ = make_client()

    response = client.get("/api/session")

    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    assert response.json() == {
        "expiresAt": "2026-09-04T10:00:00Z",
        "documentsUsed": 0,
        "documentLimit": 1000,
        "bytesUsed": 0,
        "byteLimit": 524288000,
        "questionsUsed": 0,
        "questionLimit": 30,
    }
    assert not ({"sessionKey", "token", "rawToken", "hash"} & response.json().keys())
    assert TOKEN.decode("ascii") not in response.text
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("cu_session=")
    assert "max-age=86400" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert "secure" not in cookie
    assert client.cookies.get("cu_session") is not None
    assert repository is not None


def test_cookie_contract_ignores_all_attempted_settings_overrides() -> None:
    settings = Settings.model_validate(
        {
            "app_mode": "test",
            "session_lifetime_hours": 1,
            "cookie_name": "x",
            "cookie_max_age_seconds": 1,
            "cookie_http_only": False,
            "cookie_same_site": "lax",
            "cookie_path": "/x",
            "cookie_secure": True,
        }
    )
    service = SessionService(
        MemorySessionRepository(),
        MutableClock(),
        settings=settings,
        token_factory=lambda: TOKEN,
    )
    client = TestClient(create_app(settings=settings, session_service=service))
    client.cookies.set("x", "malformed")

    response = client.get("/api/session")

    cookie = response.headers["set-cookie"].lower()
    assert response.json()["expiresAt"] == "2026-09-04T10:00:00Z"
    assert cookie.startswith("cu_session=")
    assert "max-age=86400" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert "secure" not in cookie


def test_repeat_call_reuses_cookie_without_setting_it_again() -> None:
    client, _, _ = make_client()
    first = client.get("/api/session")

    second = client.get("/api/session")

    assert first.status_code == second.status_code == 200
    assert "no-store" in second.headers["cache-control"]
    assert second.json() == first.json()
    assert "set-cookie" not in second.headers


def test_invalid_cookie_rotates() -> None:
    client, _, _ = make_client()
    client.cookies.set("cu_session", "malformed")

    response = client.get("/api/session")

    assert response.status_code == 200
    assert response.headers["set-cookie"].lower().startswith("cu_session=")


def test_expired_cookie_rotates() -> None:
    client, _, clock = make_client()
    first = client.get("/api/session")
    assert first.status_code == 200
    clock.current = NOW + timedelta(days=1)

    response = client.get("/api/session")

    assert response.status_code == 200
    assert "set-cookie" in response.headers
    assert response.json()["expiresAt"] == "2026-09-05T10:00:00Z"


def test_deployed_cookie_is_secure_with_all_other_flags() -> None:
    settings = Settings(
        app_mode="production", frontend_origin="https://frontend.example.com"
    )
    service = SessionService(
        MemorySessionRepository(),
        MutableClock(),
        settings=settings,
        token_factory=lambda: TOKEN,
    )
    client = TestClient(
        create_app(settings=Settings(app_mode="test"), session_service=service)
    )

    response = client.get("/api/session")

    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert "max-age=86400" in cookie


def test_production_mode_forces_secure_cookie() -> None:
    settings = Settings(
        app_mode="production", frontend_origin="https://frontend.example.com"
    )
    service = SessionService(
        MemorySessionRepository(),
        MutableClock(),
        settings=settings,
        token_factory=lambda: TOKEN,
    )
    client = TestClient(
        create_app(settings=Settings(app_mode="test"), session_service=service)
    )

    response = client.get("/api/session")

    assert "secure" in response.headers["set-cookie"].lower()
