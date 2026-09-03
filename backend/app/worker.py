from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar, cast

from azure.identity.aio import DefaultAzureCredential
from azure.storage.queue.aio import QueueClient
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import ConcurrencyConflict, TransientArtifactError
from app.domain.models import (
    ContentResultCleanupMessage,
    DocumentState,
    IngestionMessage,
    OutboxRecord,
)
from app.repositories.table_repository import TableApplicationRepository
from app.services.blob_service import AzureBlobStore, DocumentLeaseBusy, DocumentLeaseLost
from app.services.content_understanding import ContentUnderstandingClient, ContentUnderstandingError
from app.services.embeddings import EmbeddingError, FoundryEmbeddingClient
from app.services.ingestion_service import IngestionService
from app.services.queue_service import AzureWorkQueue
from app.services.retry import retry_delay
from app.services.search_service import AzureSearchService

LOGGER = logging.getLogger("content_understanding.worker")
MAX_ATTEMPTS = 5
VISIBILITY_TIMEOUT = 120
VISIBILITY_RENEW_INTERVAL = 60.0
MAX_CONCURRENCY = 4
POLL_INTERVAL = 1.0
M = TypeVar("M", bound=BaseModel)


class QueueMessage(Protocol):
    content: str
    dequeue_count: int | None


class ReceiveQueue(Protocol):
    async def update_message(self, message: Any, *, visibility_timeout: int) -> Any: ...
    async def delete_message(self, message: Any) -> None: ...
    async def send_message(self, content: str) -> Any: ...


def parse_ingestion_message(content: str) -> IngestionMessage:
    return IngestionMessage.model_validate_json(content)


def parse_cleanup_message(content: str) -> ContentResultCleanupMessage:
    return ContentResultCleanupMessage.model_validate_json(content)


def _safe_failure(error: Exception) -> tuple[str, bool, float | None]:
    if isinstance(error, (ContentUnderstandingError, EmbeddingError)):
        return error.code, error.retryable, error.retry_after
    if isinstance(error, (TransientArtifactError, DocumentLeaseBusy, DocumentLeaseLost)):
        return "dependency_unavailable", True, None
    return "ingestion_failed", False, None


class QueuePump[M: BaseModel]:
    def __init__(
        self,
        queue: ReceiveQueue,
        handler: Callable[[M], Awaitable[None]],
        parser: Callable[[str], M],
        *,
        poison_queue: ReceiveQueue | None = None,
        mark_failed: Callable[[M, str, bool, int], Awaitable[None]] | None = None,
        retry_forever: bool = False,
        alert: Callable[[int], None] | None = None,
        visibility_timeout: int = VISIBILITY_TIMEOUT,
        renew_interval: float = VISIBILITY_RENEW_INTERVAL,
    ) -> None:
        self._queue = queue
        self._handler = handler
        self._parser = parser
        self._poison = poison_queue
        self._mark_failed = mark_failed
        self._retry_forever = retry_forever
        self._alert = alert
        self._visibility_timeout = visibility_timeout
        self._renew_interval = renew_interval

    async def handle(self, raw: QueueMessage) -> None:
        stopped = asyncio.Event()
        operation_lock = asyncio.Lock()
        renewal = asyncio.create_task(self._renew_visibility(raw, stopped, operation_lock))
        message: M | None = None
        try:
            message = self._parser(raw.content)
            await self._handler(message)
        except Exception as error:  # noqa: BLE001 - queue boundary must classify all failures
            await self._stop_renewal(stopped, renewal)
            attempts = int(raw.dequeue_count or 1)
            code, retryable, retry_after = _safe_failure(error)
            if self._retry_forever:
                delay = int(retry_delay(attempts, retry_after, cap=3600))
                await self._update_message(raw, max(1, delay), operation_lock)
                if attempts >= MAX_ATTEMPTS and self._alert is not None:
                    self._alert(attempts)
                return
            if retryable and attempts < MAX_ATTEMPTS:
                delay = int(retry_delay(attempts, retry_after))
                await self._update_message(raw, max(1, delay), operation_lock)
                return
            if self._mark_failed is not None and message is not None:
                await self._mark_failed(message, code, retryable, attempts)
            if self._poison is not None:
                envelope = json.dumps({
                    "version": 1,
                    "documentId": str(getattr(message, "document_id", "unknown")),
                    "correlationId": str(getattr(message, "correlation_id", "unknown")),
                    "code": code,
                    "attempts": attempts,
                }, separators=(",", ":"))
                await self._poison.send_message(envelope)
            async with operation_lock:
                await self._queue.delete_message(raw)
            return
        finally:
            await self._stop_renewal(stopped, renewal)
        async with operation_lock:
            await self._queue.delete_message(raw)

    async def _renew_visibility(
        self, raw: QueueMessage, stopped: asyncio.Event, operation_lock: asyncio.Lock
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(stopped.wait(), timeout=self._renew_interval)
                return
            except TimeoutError:
                await self._update_message(raw, self._visibility_timeout, operation_lock)

    async def _update_message(
        self, raw: QueueMessage, visibility_timeout: int, operation_lock: asyncio.Lock
    ) -> None:
        async with operation_lock:
            updated = await self._queue.update_message(
                raw, visibility_timeout=visibility_timeout
            )
            if updated is not None:
                for attribute in ("pop_receipt", "next_visible_on"):
                    value = getattr(updated, attribute, None)
                    if value is not None:
                        setattr(raw, attribute, value)

    @staticmethod
    async def _stop_renewal(stopped: asyncio.Event, renewal: asyncio.Task[None]) -> None:
        stopped.set()
        with suppress(asyncio.CancelledError):
            await renewal


class CleanupProcessor:
    def __init__(self, documents: Any, content: Any, queue: Any,
                 now: Callable[[], datetime]) -> None:
        self._documents = documents
        self._content = content
        self._queue = queue
        self._now = now

    async def process(self, message: ContentResultCleanupMessage) -> None:
        for attempt in range(3):
            current = await self._documents.get(message.session_key, message.document_id)
            if current is None or self._deletion_fenced(current.value):
                return
            if (
                current.value.state is not DocumentState.RESULT_CLEANUP_PENDING
                or current.value.content_result_id != message.result_id
            ):
                return
            await self._content.delete_result(message.result_id)
            document = current.value.model_copy(update={
                "state": DocumentState.CHUNKING,
                "content_result_id": None,
                "content_operation_url": None,
                "updated_at": self._now(),
            })
            resume = IngestionMessage(
                version=1, session_key=message.session_key, document_id=message.document_id,
                blob_name=current.value.blob_name or "", correlation_id=message.correlation_id,
                enqueued_at=self._now(), resume_stage="chunking",
            )
            outbox = OutboxRecord(
                outbox_id=f"ingest:{message.document_id}:chunking:{message.result_id}",
                session_key=message.session_key,
                kind="ingestion",
                payload=resume,
                created_at=self._now(),
            )
            try:
                await self._documents.commit_document_with_outbox(document, current.etag, outbox)
            except ConcurrencyConflict:
                if attempt == 2:
                    raise
                continue
            await self._queue.enqueue_ingestion(resume)
            return

    def _deletion_fenced(self, document: Any) -> bool:
        return (
            document.state in {DocumentState.DELETING, DocumentState.DELETED}
            or document.tombstoned_at is not None
            or document.expires_at <= self._now()
        )


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


async def _receive_loop(client: Any, pump: QueuePump[Any], stop: asyncio.Event,
                        semaphore: asyncio.Semaphore) -> None:
    active: set[asyncio.Task[None]] = set()
    try:
        while not stop.is_set():
            receiver = client.receive_messages(messages_per_page=8, visibility_timeout=VISIBILITY_TIMEOUT)
            found = False
            async for message in receiver:
                found = True
                await semaphore.acquire()
                task = asyncio.create_task(_bounded_handle(pump, message, semaphore))
                active.add(task)
                task.add_done_callback(active.discard)
            if not found:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL)
                except TimeoutError:
                    pass
    finally:
        if active:
            await asyncio.gather(*active, return_exceptions=True)


async def _bounded_handle(pump: QueuePump[Any], message: QueueMessage,
                          semaphore: asyncio.Semaphore) -> None:
    try:
        await pump.handle(message)
    finally:
        semaphore.release()


async def _main(stop: asyncio.Event | None = None) -> None:
    settings = Settings()
    clock = SystemClock()
    stop_event = stop or asyncio.Event()
    credential = DefaultAzureCredential()
    account_url = f"https://{settings.storage_account_name}.queue.core.windows.net"
    ingestion_client = QueueClient(account_url, settings.ingestion_queue, credential=credential)
    cleanup_client = QueueClient(account_url, settings.content_result_cleanup_queue, credential=credential)
    poison_client = QueueClient(account_url, settings.ingestion_poison_queue, credential=credential)
    repository = TableApplicationRepository.from_settings(settings, credential=credential)
    blobs = AzureBlobStore(settings.storage_account_name, settings.uploads_container, clock,
                           derived_container=settings.derived_container,
                           control_container=settings.control_container, credential=credential)
    content = ContentUnderstandingClient(settings.foundry_endpoint, credential=credential)
    embeddings = FoundryEmbeddingClient(settings.foundry_endpoint,
                                         deployment=settings.embedding_deployment,
                                         credential=credential, release_sha=settings.release_sha)
    search = AzureSearchService(settings.search_endpoint, settings.search_index_name,
                                credential=credential)
    work_queue = AzureWorkQueue(cast(Any, ingestion_client), cast(Any, cleanup_client))
    ingestion = IngestionService(repository.documents, blobs, content, embeddings, search,
                                 work_queue, clock, analyzer_id=settings.analyzer_router_id,
                                 release_sha=settings.release_sha)

    async def mark_failed(message: IngestionMessage, code: str, retryable: bool,
                          attempts: int) -> None:
        current = await repository.documents.get(message.session_key, message.document_id)
        if current is None or current.value.tombstoned_at is not None:
            return
        failed = current.value.model_copy(update={
            "state": DocumentState.FAILED, "failure_code": code,
            "failure_retryable": retryable, "retry_count": attempts,
            "updated_at": clock.now(),
        })
        await repository.documents.replace(failed, current.etag)

    ingestion_pump = QueuePump(cast(Any, ingestion_client), ingestion.process,
                               parse_ingestion_message, poison_queue=cast(Any, poison_client),
                               mark_failed=mark_failed)
    cleanup_processor = CleanupProcessor(repository.documents, content, work_queue, clock.now)
    cleanup_pump = QueuePump(cast(Any, cleanup_client), cleanup_processor.process,
                             parse_cleanup_message, retry_forever=True,
                             alert=lambda attempt: LOGGER.error(
                                 "Content Understanding result cleanup remains pending after %s attempts",
                                 attempt,
                             ))
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    try:
        await asyncio.gather(
            _receive_loop(ingestion_client, ingestion_pump, stop_event, semaphore),
            _receive_loop(cleanup_client, cleanup_pump, stop_event, semaphore),
        )
    finally:
        await embeddings.aclose()
        await content.aclose()
        await search.aclose()
        await blobs.aclose()
        await repository.aclose()
        await ingestion_client.close()
        await cleanup_client.close()
        await poison_client.close()
        await credential.close()


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())