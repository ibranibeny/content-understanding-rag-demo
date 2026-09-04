import asyncio
import logging

from app.domain.models import ContentResultCleanupMessage, IngestionMessage, OutboxRecord
from app.domain.protocols import Clock, DocumentRepository, WorkQueue

logger = logging.getLogger(__name__)


class OutboxDispatcher:
    def __init__(
        self,
        repository: DocumentRepository,
        queue: WorkQueue,
        clock: Clock,
        *,
        interval_seconds: float = 5.0,
        batch_size: int = 100,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._clock = clock
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size

    async def dispatch_once(self) -> int:
        sent = 0
        pending = await self._repository.list_pending_outbox(self._batch_size)
        for record, etag in pending:
            if await self._dispatch(record, etag):
                sent += 1
        return sent

    async def dispatch_outbox(self, outbox_id: str) -> bool:
        pending = await self._repository.get_pending_outbox(outbox_id)
        if pending is None:
            return False
        return await self._dispatch(*pending)

    async def _dispatch(self, record: OutboxRecord, etag: str) -> bool:
        try:
            if isinstance(record.payload, IngestionMessage):
                await self._queue.enqueue_ingestion(record.payload)
            elif isinstance(record.payload, ContentResultCleanupMessage):
                await self._queue.enqueue_result_cleanup(record.payload)
            else:
                return False
            await self._repository.mark_outbox_sent(
                record.outbox_id, etag, self._clock.now()
            )
        except Exception as exc:  # noqa: BLE001 - pending row is the durable retry signal
            logger.warning(
                "outbox_dispatch_failed outbox_id=%s kind=%s exception_class=%s",
                record.outbox_id,
                record.kind,
                type(exc).__name__,
            )
            return False
        return True

    async def run(self) -> None:
        while True:
            failed = False
            try:
                await self.dispatch_once()
            except Exception as exc:  # noqa: BLE001 - next cycle retries the durable pending rows
                logger.warning(
                    "outbox_dispatch_cycle_failed exception_class=%s",
                    type(exc).__name__,
                )
                failed = True
            if failed:
                await asyncio.sleep(self._interval_seconds)
                continue
            await asyncio.sleep(self._interval_seconds)