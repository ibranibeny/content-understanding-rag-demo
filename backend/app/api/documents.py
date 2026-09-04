from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.dependencies import require_expected_origin, resolve_session_cookie
from app.domain.models import DocumentDeleteResponse, DocumentResponse, DocumentSummaryResponse
from app.services.document_service import DocumentService
from app.services.session_service import SessionService

router = APIRouter(prefix="/api/documents", tags=["documents"])


def get_session_service(request: Request) -> SessionService:
    return cast(SessionService, request.app.state.session_service)


def get_document_service(request: Request) -> DocumentService:
    return cast(DocumentService, request.app.state.document_service)


@router.get("", response_model=list[DocumentSummaryResponse])
async def list_documents(
    request: Request,
    response: Response,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    documents: Annotated[DocumentService, Depends(get_document_service)],
) -> list[DocumentSummaryResponse]:
    return await documents.list(await resolve_session_cookie(request, response, sessions))


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: UUID,
    request: Request,
    response: Response,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    documents: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentResponse:
    session_key = await resolve_session_cookie(request, response, sessions)
    return await documents.get(session_key, document_id)


@router.post(
    "/{document_id}/retry",
    response_model=DocumentResponse,
    dependencies=[Depends(require_expected_origin)],
)
async def retry_document(
    document_id: UUID,
    request: Request,
    response: Response,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    documents: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentResponse:
    session_key = await resolve_session_cookie(request, response, sessions)
    return await documents.retry(session_key, document_id, UUID(request.state.correlation_id))


@router.delete(
    "/{document_id}",
    response_model=DocumentDeleteResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_expected_origin)],
)
async def delete_document(
    document_id: UUID,
    request: Request,
    response: Response,
    sessions: Annotated[SessionService, Depends(get_session_service)],
    documents: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentDeleteResponse:
    session_key = await resolve_session_cookie(request, response, sessions)
    return await documents.delete(session_key, document_id)