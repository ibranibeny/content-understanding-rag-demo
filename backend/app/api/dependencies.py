from fastapi import Request, Response

from app.core.config import (
    SESSION_COOKIE_HTTP_ONLY,
    SESSION_COOKIE_MAX_AGE_SECONDS,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    SESSION_COOKIE_SAME_SITE,
)
from app.core.errors import AppError
from app.services.session_service import SessionService


async def require_expected_origin(request: Request) -> None:
    """Reject a mutation unless it has exactly one configured Origin value."""
    origins = request.headers.getlist("origin")
    if len(origins) != 1 or origins[0] != request.app.state.settings.frontend_origin:
        raise AppError(
            "invalid_origin",
            403,
            "The request origin is not allowed.",
            False,
        )


async def resolve_session_cookie(
    request: Request, response: Response, service: SessionService
) -> str:
    response.headers["Cache-Control"] = "no-store"
    resolved = await service.resolve(request.cookies.get(SESSION_COOKIE_NAME))
    if resolved.is_new:
        assert resolved.raw_token is not None
        request.state.rotated_session_token = resolved.raw_token
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=resolved.raw_token,
            max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
            secure=service.settings.app_mode == "production",
            httponly=SESSION_COOKIE_HTTP_ONLY,
            samesite=SESSION_COOKIE_SAME_SITE,
            path=SESSION_COOKIE_PATH,
        )
    return resolved.record.session_key