import logging
from uuid import UUID

from app.core.errors import AppError, ConcurrencyConflict
from app.domain.models import (
    DocumentDeleteResponse,
    DocumentRecord,
    DocumentResponse,
    DocumentState,
    DocumentSummaryResponse,
    IngestionMessage,
    OutboxRecord,
)
from app.domain.protocols import Clock, DocumentRepository
from app.services.deletion_service import DeletionService
from app.services.upload_service import Dispatcher

MAX_ETAG_RETRIES = 5
HIDDEN_STATES = frozenset({DocumentState.DELETING, DocumentState.DELETED})
logger = logging.getLogger(__name__)


class DocumentService:
    def __init__(
        self,
        repository: DocumentRepository,
        deletion: DeletionService,
        dispatcher: Dispatcher,
        clock: Clock,
    ) -> None:
        self._repository = repository
        self._deletion = deletion
        self._dispatcher = dispatcher
        self._clock = clock
        self.retry_calls = 0
        self.delete_calls = 0

    @property
    def repository(self) -> DocumentRepository:
        return self._repository

    @property
    def deletion_service(self) -> DeletionService:
        return self._deletion

    async def list(self, session_key: str) -> list[DocumentSummaryResponse]:
        documents = await self._repository.list_for_session(session_key)
        visible = [item.value for item in documents if item.value.state not in HIDDEN_STATES]
        visible.sort(key=lambda item: (-item.created_at.timestamp(), str(item.document_id)))
        return [self._summary(document) for document in visible]

    async def get(self, session_key: str, document_id: UUID) -> DocumentResponse:
        current = await self._repository.get(session_key, document_id)
        if current is None or current.value.state in HIDDEN_STATES:
            raise self._not_found()
        return self._detail(current.value)

    async def retry(
        self, session_key: str, document_id: UUID, correlation_id: UUID
    ) -> DocumentResponse:
        self.retry_calls += 1
        for _ in range(MAX_ETAG_RETRIES):
            current = await self._repository.get(session_key, document_id)
            if current is None:
                raise self._not_found()
            document = current.value
            if (
                document.state is DocumentState.QUEUED
                and document.retry_count > 1
                and document.expires_at > self._clock.now()
            ):
                await self._try_dispatch(document)
                return self._detail(document)
            if (
                document.state is not DocumentState.FAILED
                or not document.failure_retryable
                or document.expires_at <= self._clock.now()
                or document.tombstoned_at is not None
            ):
                raise self._invalid_state()
            now = self._clock.now()
            next_attempt = document.retry_count + 1
            queued = document.model_copy(
                update={
                    "state": DocumentState.QUEUED,
                    "updated_at": now,
                    "failure_code": None,
                    "failure_retryable": False,
                    "retry_count": next_attempt,
                }
            )
            outbox = OutboxRecord(
                outbox_id=self._outbox_id(document.document_id, next_attempt),
                session_key=session_key,
                kind="ingestion",
                payload=IngestionMessage(
                    version=1,
                    session_key=session_key,
                    document_id=document.document_id,
                    blob_name=document.blob_name,
                    correlation_id=correlation_id,
                    enqueued_at=now,
                ),
                created_at=now,
            )
            try:
                committed = await self._repository.commit_queued_with_outbox(
                    queued, current.etag, outbox
                )
            except ConcurrencyConflict:
                continue
            await self._try_dispatch(committed.value)
            return self._detail(committed.value)
        refreshed = await self._repository.get(session_key, document_id)
        if (
            refreshed is not None
            and refreshed.value.state is DocumentState.QUEUED
            and refreshed.value.retry_count > 1
        ):
            await self._try_dispatch(refreshed.value)
            return self._detail(refreshed.value)
        raise AppError(
            "document_update_conflict", 409, "The document changed. Retry the request.", True
        )

    async def delete(self, session_key: str, document_id: UUID) -> DocumentDeleteResponse:
        self.delete_calls += 1
        deleted = await self._deletion.request_delete(
            session_key, document_id, self._clock.now()
        )
        if deleted.value.state not in {DocumentState.DELETING, DocumentState.DELETED}:
            raise RuntimeError("deletion service returned a visible document state")
        return DocumentDeleteResponse(
            document_id=deleted.value.document_id,
            state=deleted.value.state,
            updated_at=deleted.value.updated_at,
        )

    async def _try_dispatch(self, document: DocumentRecord) -> None:
        try:
            await self._dispatcher.dispatch_outbox(
                self._outbox_id(document.document_id, document.retry_count)
            )
        except Exception as exc:  # noqa: BLE001 - durable outbox remains pending
            logger.warning(
                "document_retry_dispatch_failed exception_class=%s", type(exc).__name__
            )

    @staticmethod
    def _outbox_id(document_id: UUID, attempt: int) -> str:
        return f"ingest:{document_id}:{attempt}"

    @staticmethod
    def _summary(document: DocumentRecord) -> DocumentSummaryResponse:
        return DocumentSummaryResponse.model_validate(document, from_attributes=True)

    @staticmethod
    def _detail(document: DocumentRecord) -> DocumentResponse:
        return DocumentResponse.model_validate(document, from_attributes=True)

    @staticmethod
    def _not_found() -> AppError:
        return AppError("document_not_found", 404, "The document was not found.", False)

    @staticmethod
    def _invalid_state() -> AppError:
        return AppError(
            "invalid_document_state",
            409,
            "The document cannot be retried in this state.",
            False,
        )