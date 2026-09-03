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
    UploadInitRequest,
    UploadInitResponse,
)
from app.domain.protocols import Clock, DocumentRepository, UploadBlobStore
from app.services.file_validation import validate_declared_upload, validate_uploaded_file
from app.services.session_service import SessionService


class Dispatcher(Protocol):
    async def dispatch_once(self) -> int: ...


class UploadService:
    def __init__(
        self,
        sessions: SessionService,
        documents: DocumentRepository,
        blobs: UploadBlobStore,
        dispatcher: Dispatcher,
        clock: Clock,
        *,
        document_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._sessions = sessions
        self._documents = documents
        self._blobs = blobs
        self._dispatcher = dispatcher
        self._clock = clock
        self._document_id_factory = document_id_factory

    async def initialize(
        self, session_key: str, request: UploadInitRequest
    ) -> UploadInitResponse:
        declared = validate_declared_upload(
            request.file_name,
            request.content_type,
            request.size_bytes,
            max_file_bytes=self._sessions.settings.max_file_bytes,
        )
        session = await self._sessions.reserve_document(session_key, declared.size_bytes)
        document_id = self._document_id_factory()
        now = self._clock.now()
        blob_name = f"uploads/{session_key}/{document_id}/{declared.file_name}"
        document = DocumentRecord(
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
            created = await self._documents.create(document)
        except Exception as create_error:
            try:
                await self._sessions.release_document(session_key, declared.size_bytes)
            except Exception as rollback_error:  # noqa: BLE001 - retain the stable API boundary
                create_error = ExceptionGroup(
                    "document creation and quota rollback failed",
                    [create_error, rollback_error],
                )
            raise AppError(
                "document_create_failed",
                503,
                "The document upload could not be initialized.",
                True,
            ) from create_error
        try:
            grant = await self._blobs.create_upload(blob_name, declared.content_type)
        except Exception as grant_error:  # noqa: BLE001 - complete compensation follows
            compensation_errors: list[Exception] = []
            document_deleted = False
            try:
                await self._documents.delete(session_key, document_id, created.etag)
                document_deleted = True
            except Exception as exc:  # noqa: BLE001 - quota compensation must still run
                compensation_errors.append(exc)
            if document_deleted:
                try:
                    await self._sessions.release_document(session_key, declared.size_bytes)
                except Exception as exc:  # noqa: BLE001 - stable boundary error is mandatory
                    compensation_errors.append(exc)
            internal_cause: BaseException = grant_error
            if compensation_errors:
                internal_cause = ExceptionGroup(
                    "upload grant compensation failed", compensation_errors
                )
            raise AppError(
                "upload_grant_failed",
                503,
                "The upload authorization could not be created.",
                True,
            ) from internal_cause
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
        if document.state not in {DocumentState.AWAITING_UPLOAD, DocumentState.FAILED}:
            await self._try_dispatch()
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
        validate_uploaded_file(declared, verified.header, verified.package)
        now = self._clock.now()
        queued = document.model_copy(update={"state": DocumentState.QUEUED, "updated_at": now})
        message = IngestionMessage(
            version=1,
            session_key=session_key,
            document_id=document_id,
            blob_name=document.blob_name,
            correlation_id=correlation_id,
            enqueued_at=now,
        )
        outbox = OutboxRecord(
            outbox_id=f"ingest:{document_id}:1",
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
            if current.value.state != DocumentState.QUEUED:
                raise AppError(
                    "invalid_document_state",
                    409,
                    "The document cannot be completed in this state.",
                    False,
                ) from None
            committed = current
        await self._try_dispatch()
        return self._response(committed.value)

    async def _try_dispatch(self) -> None:
        try:
            await self._dispatcher.dispatch_once()
        except Exception:  # noqa: BLE001 - the durable outbox is retried by the lifespan loop
            return

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
            created_at=document.created_at,
            updated_at=document.updated_at,
            expires_at=document.expires_at,
        )