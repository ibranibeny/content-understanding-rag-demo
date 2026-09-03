from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response

from app.core.config import (
    SESSION_COOKIE_HTTP_ONLY,
    SESSION_COOKIE_MAX_AGE_SECONDS,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    SESSION_COOKIE_SAME_SITE,
)
from app.domain.models import SessionResponse
from app.services.session_service import SessionService

router = APIRouter(prefix="/api", tags=["session"])


def get_session_service(request: Request) -> SessionService:
    return cast(SessionService, request.app.state.session_service)


@router.get("/session", response_model=SessionResponse)
async def get_session(
    request: Request,
    response: Response,
    service: Annotated[SessionService, Depends(get_session_service)],
) -> SessionResponse:
    settings = request.app.state.settings
    response.headers["Cache-Control"] = "no-store"
    raw_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    resolved = await service.resolve(raw_cookie)
    if resolved.is_new:
        assert resolved.raw_token is not None
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=resolved.raw_token,
            max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
            secure=settings.app_mode == "production",
            httponly=SESSION_COOKIE_HTTP_ONLY,
            samesite=SESSION_COOKIE_SAME_SITE,
            path=SESSION_COOKIE_PATH,
        )
    return service.response_for(resolved.record)
