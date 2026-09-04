from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import require_expected_origin, resolve_session_cookie
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


@router.post(
    "/init",
    response_model=UploadInitResponse,
    dependencies=[Depends(require_expected_origin)],
)
async def initialize_upload(
    body: UploadInitRequest,
    request: Request,
    response: Response,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    uploads: Annotated[UploadService, Depends(get_upload_service)],
) -> UploadInitResponse:
    session_key = await resolve_session_cookie(request, response, sessions)
    return await uploads.initialize(session_key, body)


@router.post(
    "/{document_id}/complete",
    response_model=DocumentResponse,
    dependencies=[Depends(require_expected_origin)],
)
async def complete_upload(
    document_id: UUID,
    body: UploadCompleteRequest,
    request: Request,
    response: Response,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    uploads: Annotated[UploadService, Depends(get_upload_service)],
) -> DocumentResponse:
    session_key = await resolve_session_cookie(request, response, sessions)
    correlation_id = UUID(request.state.correlation_id)
    return await uploads.complete(session_key, document_id, body.etag, correlation_id)