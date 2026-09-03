import logging
from collections.abc import Callable
from typing import Protocol
from uuid import UUID, uuid4

from app.core.errors import AppError, ConcurrencyConflict
from app.domain.models import (
    DocumentRecord,
    DocumentResponse,
    DocumentState,
    IngestionMessage,
    OutboxRecord,
    SessionRecord,
    UploadInitRequest,
    UploadInitResponse,
)
from app.domain.protocols import Clock, DocumentRepository, IngestionBacklog, UploadBlobStore
from app.services.file_validation import validate_declared_upload, validate_uploaded_file
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)

COMPLETED_UPLOAD_STATES = frozenset(
    {
        DocumentState.QUEUED,
        DocumentState.ANALYZING,
        DocumentState.CLASSIFIED,
        DocumentState.EXTRACTED,
        DocumentState.RESULT_CLEANUP_PENDING,
        DocumentState.CHUNKING,
        DocumentState.EMBEDDING,
        DocumentState.INDEXING,
        DocumentState.READY,
    }
)


class Dispatcher(Protocol):
    async def dispatch_outbox(self, outbox_id: str) -> bool: ...


class UploadService:
    def __init__(
        self,
        sessions: SessionService,
        documents: DocumentRepository,
        blobs: UploadBlobStore,
        dispatcher: Dispatcher,
        clock: Clock,
        backlog: IngestionBacklog,
        *,
        document_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._sessions = sessions
        self._documents = documents
        self._blobs = blobs
        self._dispatcher = dispatcher
        self._clock = clock
        self._backlog = backlog
        self._document_id_factory = document_id_factory

    @property
    def documents(self) -> DocumentRepository:
        return self._documents

    @property
    def blobs(self) -> UploadBlobStore:
        return self._blobs

    async def initialize(
        self, session_key: str, request: UploadInitRequest
    ) -> UploadInitResponse:
        declared = validate_declared_upload(
            request.file_name,
            request.content_type,
            request.size_bytes,
            max_file_bytes=self._sessions.settings.max_file_bytes,
        )
        await self._sessions.require_active(session_key)
        try:
            backlog = await self._backlog.get_ingestion_backlog()
        except Exception as backlog_error:
            raise AppError(
                "ingestion_backlog_unavailable",
                503,
                "Upload capacity is temporarily unavailable.",
                True,
            ) from backlog_error
        if backlog < 0:
            raise AppError(
                "ingestion_backlog_unavailable",
                503,
                "Upload capacity is temporarily unavailable.",
                True,
            )
        if backlog >= self._sessions.settings.max_ingestion_backlog:
            raise AppError(
                "ingestion_backlog_full",
                503,
                "Upload capacity is temporarily full. Try again later.",
                True,
            )
        document_id = self._document_id_factory()
        now = self._clock.now()
        blob_name = f"uploads/{session_key}/{document_id}/{declared.file_name}"
        try:
            grant = await self._blobs.create_upload(blob_name, declared.content_type)
        except Exception as grant_error:
            raise AppError(
                "upload_grant_failed",
                503,
                "The upload authorization could not be created.",
                True,
            ) from grant_error

        def build_document(session: SessionRecord) -> DocumentRecord:
            return DocumentRecord(
                session_key=session_key,
                document_id=document_id,
                file_name=declared.file_name,
                content_type=declared.content_type,
                size_bytes=declared.size_bytes,
                blob_name=blob_name,
                state=DocumentState.AWAITING_UPLOAD,
                created_at=now,
                updated_at=now,
                expires_at=session.expires_at,
            )

        try:
            await self._sessions.reserve_and_create(
                session_key, declared.size_bytes, build_document
            )
        except AppError:
            raise
        except Exception as create_error:
            raise AppError(
                "document_create_failed",
                503,
                "The document upload could not be initialized.",
                True,
            ) from create_error
        return UploadInitResponse(
            upload_url=grant.upload_url,
            document_id=document_id,
            expires_at=grant.expires_at,
            required_headers=dict(grant.required_headers),
        )

    async def complete(
        self,
        session_key: str,
        document_id: UUID,
        expected_etag: str,
        correlation_id: UUID,
    ) -> DocumentResponse:
        versioned = await self._documents.get(session_key, document_id)
        if versioned is None:
            raise AppError("document_not_found", 404, "The document was not found.", False)
        document = versioned.value
        outbox_id = f"ingest:{document_id}:1"
        if document.state in COMPLETED_UPLOAD_STATES:
            await self._try_dispatch(outbox_id)
            return self._response(document)
        if document.state != DocumentState.AWAITING_UPLOAD:
            raise AppError(
                "invalid_document_state", 409, "The document cannot be completed in this state.", False
            )

        declared = validate_declared_upload(
            document.file_name,
            document.content_type,
            document.size_bytes,
            max_file_bytes=self._sessions.settings.max_file_bytes,
        )
        verified = await self._blobs.verify_upload(
            document.blob_name,
            expected_etag,
            document.size_bytes,
            document.content_type,
            office=declared.is_office,
        )
        validate_uploaded_file(
            declared, verified.header, verified.package, verified.office_summary
        )
        now = self._clock.now()
        queued = document.model_copy(
            update={"state": DocumentState.QUEUED, "updated_at": now, "retry_count": 1}
        )
        message = IngestionMessage(
            version=1,
            session_key=session_key,
            document_id=document_id,
            blob_name=document.blob_name,
            correlation_id=correlation_id,
            enqueued_at=now,
        )
        outbox = OutboxRecord(
            outbox_id=outbox_id,
            session_key=session_key,
            kind="ingestion",
            payload=message,
            created_at=now,
        )
        try:
            committed = await self._documents.commit_queued_with_outbox(
                queued, versioned.etag, outbox
            )
        except ConcurrencyConflict:
            current = await self._documents.get(session_key, document_id)
            if current is None or current.value.state == DocumentState.AWAITING_UPLOAD:
                raise AppError(
                    "concurrency_conflict",
                    503,
                    "The document changed concurrently. Retry the request.",
                    True,
                ) from None
            if current.value.state not in COMPLETED_UPLOAD_STATES:
                raise AppError(
                    "invalid_document_state",
                    409,
                    "The document cannot be completed in this state.",
                    False,
                ) from None
            committed = current
        await self._try_dispatch(outbox_id)
        return self._response(committed.value)

    async def _try_dispatch(self, outbox_id: str) -> None:
        try:
            await self._dispatcher.dispatch_outbox(outbox_id)
        except Exception as exc:  # noqa: BLE001 - durable outbox remains pending
            logger.warning(
                "opportunistic_outbox_dispatch_failed exception_class=%s",
                type(exc).__name__,
            )
            return

    async def aclose(self) -> None:
        await self._blobs.aclose()

    @staticmethod
    def _response(document: DocumentRecord) -> DocumentResponse:
        return DocumentResponse(
            document_id=document.document_id,
            file_name=document.file_name,
            state=document.state,
            document_type=document.document_type,
            title=document.title,
            page_count=document.page_count,
            chunk_count=document.chunk_count,
            token_count=document.token_count,
            extraction=document.extraction,
            failure_code=document.failure_code,
            failure_retryable=document.failure_retryable,
            retry_count=document.retry_count,
            created_at=document.created_at,
            updated_at=document.updated_at,
            expires_at=document.expires_at,
        )