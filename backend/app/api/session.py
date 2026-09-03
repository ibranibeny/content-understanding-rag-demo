from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response

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
    raw_cookie = request.cookies.get(settings.cookie_name)
    resolved = await service.resolve(raw_cookie)
    if resolved.is_new:
        assert resolved.raw_token is not None
        response.set_cookie(
            key=settings.cookie_name,
            value=resolved.raw_token,
            max_age=settings.cookie_max_age_seconds,
            secure=settings.cookie_secure or settings.app_mode == "production",
            httponly=settings.cookie_http_only,
            samesite=settings.cookie_same_site,
            path=settings.cookie_path,
        )
    return service.response_for(resolved.record)