import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.domain.models import IngestionMessage, OutboxRecord
from app.repositories.memory_repository import MemoryDocumentRepository
from app.services.outbox_service import OutboxDispatcher

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
SESSION_KEY = "a" * 64
DOCUMENT_ID = UUID("11111111-1111-4111-8111-111111111111")


class Clock:
    def now(self) -> datetime:
        return NOW


class Queue:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[IngestionMessage] = []

    async def enqueue_ingestion(self, message: IngestionMessage) -> None:
        if self.fail:
            raise RuntimeError("simulated queue failure")
        self.messages.append(message)

    async def enqueue_result_cleanup(self, message: object) -> None:
        raise AssertionError(message)


class MarkFailsOnceRepository(MemoryDocumentRepository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_mark = True

    async def mark_outbox_sent(self, outbox_id: str, etag: str, sent_at: datetime) -> None:
        if self.fail_mark:
            self.fail_mark = False
            raise RuntimeError("simulated crash after queue send")
        await super().mark_outbox_sent(outbox_id, etag, sent_at)


class ListFailsOnceRepository(MemoryDocumentRepository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_list = True

    async def list_pending_outbox(self, limit: int):  # type: ignore[no-untyped-def]
        if self.fail_list:
            self.fail_list = False
            raise RuntimeError("storage unavailable")
        return await super().list_pending_outbox(limit)


def outbox() -> OutboxRecord:
    message = IngestionMessage(
        version=1,
        session_key=SESSION_KEY,
        document_id=DOCUMENT_ID,
        blob_name=f"uploads/{SESSION_KEY}/{DOCUMENT_ID}/a.pdf",
        correlation_id=UUID("22222222-2222-4222-8222-222222222222"),
        enqueued_at=NOW,
    )
    return OutboxRecord(
        outbox_id=f"ingest:{DOCUMENT_ID}:1",
        session_key=SESSION_KEY,
        kind="ingestion",
        payload=message,
        created_at=NOW,
    )


async def seed(repository: MemoryDocumentRepository) -> None:
    await repository.put_outbox_for_test(outbox())


async def test_crash_before_send_leaves_pending() -> None:
    repository = MemoryDocumentRepository()
    await seed(repository)
    dispatcher = OutboxDispatcher(repository, Queue(fail=True), Clock(), interval_seconds=0.01)

    assert await dispatcher.dispatch_once() == 0
    pending = await repository.list_pending_outbox(10)
    assert [item.outbox_id for item, _ in pending] == [f"ingest:{DOCUMENT_ID}:1"]


async def test_crash_after_send_can_duplicate_same_deterministic_message() -> None:
    repository = MarkFailsOnceRepository()
    await seed(repository)
    queue = Queue()
    dispatcher = OutboxDispatcher(repository, queue, Clock(), interval_seconds=0.01)

    assert await dispatcher.dispatch_once() == 0
    assert await dispatcher.dispatch_once() == 1

    assert len(queue.messages) == 2
    assert queue.messages[0] == queue.messages[1]
    assert json.loads(queue.messages[0].model_dump_json()) == {
        "version": 1,
        "sessionKey": SESSION_KEY,
        "documentId": str(DOCUMENT_ID),
        "blobName": f"uploads/{SESSION_KEY}/{DOCUMENT_ID}/a.pdf",
        "correlationId": "22222222-2222-4222-8222-222222222222",
        "enqueuedAt": "2026-09-03T10:00:00Z",
        "resumeStage": "analyzing",
    }
    assert await repository.list_pending_outbox(10) == []


async def test_sent_outbox_is_not_dispatched_again() -> None:
    repository = MemoryDocumentRepository()
    await seed(repository)
    queue = Queue()
    dispatcher = OutboxDispatcher(repository, queue, Clock())

    assert await dispatcher.dispatch_once() == 1
    assert await dispatcher.dispatch_once() == 0
    assert len(queue.messages) == 1
    stored = await repository.all_outbox_for_test()
    assert stored[0].sent_at == NOW


async def test_dispatcher_cancellation_finishes_cleanly() -> None:
    dispatcher = OutboxDispatcher(MemoryDocumentRepository(), Queue(), Clock(), interval_seconds=60)
    task = asyncio.create_task(dispatcher.run())
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_background_dispatcher_survives_transient_list_failure() -> None:
    repository = ListFailsOnceRepository()
    await seed(repository)
    queue = Queue()
    dispatcher = OutboxDispatcher(repository, queue, Clock(), interval_seconds=0)
    task = asyncio.create_task(dispatcher.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if queue.messages:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(queue.messages) == 1