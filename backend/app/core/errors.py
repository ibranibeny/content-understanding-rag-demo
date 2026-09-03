from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.core.config import (
    SESSION_COOKIE_HTTP_ONLY,
    SESSION_COOKIE_MAX_AGE_SECONDS,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    SESSION_COOKIE_SAME_SITE,
)

CORRELATION_HEADER = "X-Correlation-ID"


class ConcurrencyConflict(Exception):
    """A repository write was rejected because its ETag was stale."""


class RepositoryDataError(Exception):
    """Persisted repository data could not be decoded safely."""

    def __init__(self) -> None:
        super().__init__("Stored table entity is invalid.")


class TransientArtifactError(Exception):
    """A Blob or Search artifact operation can be retried safely."""


@dataclass(frozen=True, slots=True)
class AppError(Exception):
    code: str
    status_code: int
    message: str
    retryable: bool


async def correlation_middleware(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    raw_correlation_id = request.headers.get(CORRELATION_HEADER)
    try:
        correlation_id = str(UUID(raw_correlation_id)) if raw_correlation_id else str(uuid4())
    except (ValueError, AttributeError):
        correlation_id = str(uuid4())

    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers[CORRELATION_HEADER] = correlation_id
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):
        raise exc
    correlation_id = getattr(request.state, "correlation_id", str(uuid4()))
    envelope: dict[str, Any] = {
        "error": {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "correlationId": correlation_id,
        }
    }
    response = JSONResponse(
        status_code=exc.status_code,
        content=envelope,
        headers={"Cache-Control": "no-store"},
    )
    rotated_token = getattr(request.state, "rotated_session_token", None)
    if isinstance(rotated_token, str):
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=rotated_token,
            max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
            secure=request.app.state.settings.app_mode == "production",
            httponly=SESSION_COOKIE_HTTP_ONLY,
            samesite=SESSION_COOKIE_SAME_SITE,
            path=SESSION_COOKIE_PATH,
        )
    return response


async def request_validation_error_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    correlation_id = getattr(request.state, "correlation_id", str(uuid4()))
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "invalid_request",
                "message": "The request is invalid.",
                "retryable": False,
                "correlationId": correlation_id,
            }
        },
    )
