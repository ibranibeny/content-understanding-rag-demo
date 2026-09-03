from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import AppError
from app.main import create_app


def app_with_error_route() -> FastAPI:
    app = create_app()

    @app.get("/test-error")
    async def test_error() -> None:
        raise AppError("file_too_large", 413, "Files must be 100 MB or smaller.", False)

    return app


def test_app_error_uses_stable_envelope_and_incoming_correlation_id() -> None:
    correlation_id = "868fba2c-1695-42d4-af7f-79069e434b34"
    response = TestClient(app_with_error_route()).get(
        "/test-error", headers={"X-Correlation-ID": correlation_id}
    )

    assert response.status_code == 413
    assert response.headers["X-Correlation-ID"] == correlation_id
    assert response.json() == {
        "error": {
            "code": "file_too_large",
            "message": "Files must be 100 MB or smaller.",
            "retryable": False,
            "correlationId": correlation_id,
        }
    }


def test_invalid_correlation_id_is_replaced_and_shared_with_error() -> None:
    response = TestClient(app_with_error_route()).get(
        "/test-error", headers={"X-Correlation-ID": "not-a-uuid"}
    )

    generated = response.headers["X-Correlation-ID"]
    UUID(generated)
    assert response.json()["error"]["correlationId"] == generated
    assert "AppError" not in response.text
