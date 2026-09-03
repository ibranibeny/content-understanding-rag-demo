from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.core.config import (
    SESSION_COOKIE_HTTP_ONLY,
    SESSION_COOKIE_MAX_AGE_SECONDS,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    SESSION_COOKIE_SAME_SITE,
)
from app.domain.models import (
    DocumentResponse,
    UploadCompleteRequest,
    UploadInitRequest,
    UploadInitResponse,
)
from app.services.session_service import SessionService
from app.services.upload_service import UploadService

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


def get_session_service(request: Request) -> SessionService:
    return cast(SessionService, request.app.state.session_service)


def get_upload_service(request: Request) -> UploadService:
    return cast(UploadService, request.app.state.upload_service)


async def _resolve_session(request: Request, response: Response, service: SessionService) -> str:
    response.headers["Cache-Control"] = "no-store"
    resolved = await service.resolve(request.cookies.get(SESSION_COOKIE_NAME))
    if resolved.is_new:
        assert resolved.raw_token is not None
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=resolved.raw_token,
            max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
            secure=request.app.state.settings.app_mode == "production",
            httponly=SESSION_COOKIE_HTTP_ONLY,
            samesite=SESSION_COOKIE_SAME_SITE,
            path=SESSION_COOKIE_PATH,
        )
    return resolved.record.session_key


@router.post("/init", response_model=UploadInitResponse)
async def initialize_upload(
    body: UploadInitRequest,
    request: Request,
    response: Response,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    uploads: Annotated[UploadService, Depends(get_upload_service)],
) -> UploadInitResponse:
    session_key = await _resolve_session(request, response, sessions)
    return await uploads.initialize(session_key, body)


@router.post("/{document_id}/complete", response_model=DocumentResponse)
async def complete_upload(
    document_id: UUID,
    body: UploadCompleteRequest,
    request: Request,
    response: Response,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    uploads: Annotated[UploadService, Depends(get_upload_service)],
) -> DocumentResponse:
    session_key = await _resolve_session(request, response, sessions)
    correlation_id = UUID(request.state.correlation_id)
    return await uploads.complete(session_key, document_id, body.etag, correlation_id)