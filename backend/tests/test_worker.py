import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.domain.models import ContentResultCleanupMessage, IngestionMessage
from app.services.content_understanding import ContentUnderstandingError
from app.worker import CleanupProcessor, QueuePump, parse_cleanup_message, parse_ingestion_message

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
SESSION = "a" * 64
DOCUMENT = UUID("9f4b8484-9f6b-44f2-b4d4-e5e7687c80df")
CORRELATION = UUID("868fba2c-1695-42d4-af7f-79069e434b34")


def ingestion() -> IngestionMessage:
    return IngestionMessage(version=1, session_key=SESSION, document_id=DOCUMENT,
                            blob_name="uploads/input.pdf", correlation_id=CORRELATION,
                            enqueued_at=NOW)


def cleanup() -> ContentResultCleanupMessage:
    return ContentResultCleanupMessage(version=1, session_key=SESSION, document_id=DOCUMENT,
                                       result_id="result-1", correlation_id=CORRELATION,
                                       enqueued_at=NOW)


def test_queue_message_serialization_round_trips_and_rejects_unknown_fields() -> None:
    assert parse_ingestion_message(ingestion().model_dump_json(by_alias=True)) == ingestion()
    assert parse_cleanup_message(cleanup().model_dump_json(by_alias=True)) == cleanup()
    raw = json.loads(ingestion().model_dump_json(by_alias=True))
    raw["secret"] = "must-not-pass"
    with pytest.raises(ValidationError):
        parse_ingestion_message(json.dumps(raw))


class Queue:
    def __init__(self, message) -> None:  # type: ignore[no-untyped-def]
        self.message = message
        self.deleted = 0
        self.updates: list[int] = []
        self.poison: list[str] = []

    async def update_message(self, message, *, visibility_timeout: int):  # type: ignore[no-untyped-def]
        del message
        self.updates.append(visibility_timeout)

    async def delete_message(self, message) -> None:  # type: ignore[no-untyped-def]
        del message
        self.deleted += 1

    async def send_message(self, content: str) -> None:
        self.poison.append(content)


async def test_queue_pump_renews_visibility_and_deletes_only_after_success() -> None:
    queue = Queue(SimpleNamespace(content=ingestion().model_dump_json(by_alias=True), dequeue_count=1))
    started = asyncio.Event()
    finish = asyncio.Event()

    async def handler(message: IngestionMessage) -> None:
        del message
        started.set()
        await finish.wait()

    pump = QueuePump(queue, handler, parse_ingestion_message, visibility_timeout=1,
                     renew_interval=0.01)
    task = asyncio.create_task(pump.handle(queue.message))
    await started.wait()
    await asyncio.sleep(0.03)
    assert queue.deleted == 0
    assert queue.updates
    finish.set()
    await task
    assert queue.deleted == 1


async def test_normal_failure_attempt_five_marks_failed_and_sends_sanitized_poison() -> None:
    raw = ingestion().model_dump_json(by_alias=True)
    message = SimpleNamespace(content=raw, dequeue_count=5)
    queue, poison = Queue(message), Queue(message)
    failures: list[tuple[str, bool, int]] = []

    async def handler(parsed: IngestionMessage) -> None:
        del parsed
        raise ContentUnderstandingError("content_understanding_unavailable", retryable=True)

    async def mark_failed(parsed: IngestionMessage, code: str, retryable: bool, attempts: int) -> None:
        del parsed
        failures.append((code, retryable, attempts))

    pump = QueuePump(queue, handler, parse_ingestion_message, poison_queue=poison,
                     mark_failed=mark_failed, renew_interval=60)
    await pump.handle(message)
    assert failures == [("content_understanding_unavailable", True, 5)]
    assert queue.deleted == 1 and len(poison.poison) == 1
    assert "sig=" not in poison.poison[0]


class Documents:
    def __init__(self) -> None:
        self.cleared = False
        self.outbox = None

    async def get(self, session_key, document_id):  # type: ignore[no-untyped-def]
        del session_key, document_id
        from app.domain.models import DocumentRecord, DocumentState
        document = DocumentRecord(
            session_key=SESSION, document_id=DOCUMENT, file_name="a.pdf",
            content_type="application/pdf", size_bytes=1, blob_name="b",
            state=DocumentState.RESULT_CLEANUP_PENDING, created_at=NOW, updated_at=NOW,
            expires_at=datetime(2026, 9, 4, tzinfo=UTC), content_result_id="result-1",
            content_operation_url="https://cu/op/1",
        )
        return SimpleNamespace(value=document, etag='W/"1"')

    async def commit_document_with_outbox(self, document, etag, outbox):  # type: ignore[no-untyped-def]
        del document, etag
        self.cleared = True
        self.outbox = outbox


class WorkQueue:
    def __init__(self) -> None:
        self.ingestion: list[IngestionMessage] = []

    async def enqueue_ingestion(self, message: IngestionMessage) -> None:
        self.ingestion.append(message)


class CU:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0

    async def delete_result(self, result_id: str) -> None:
        del result_id
        self.calls += 1
        if self.calls <= self.failures:
            raise ContentUnderstandingError("unavailable", retryable=True, retry_after=4)


async def test_cleanup_consumer_only_deletes_then_requeues_chunking() -> None:
    documents, queue, cu = Documents(), WorkQueue(), CU()
    processor = CleanupProcessor(documents, cu, queue, lambda: NOW)
    await processor.process(cleanup())
    assert cu.calls == 1 and documents.cleared
    assert queue.ingestion[0].resume_stage == "chunking"


async def test_cleanup_failure_never_poisons_and_uses_increasing_visibility_after_five() -> None:
    message = SimpleNamespace(content=cleanup().model_dump_json(by_alias=True), dequeue_count=7)
    queue, cu = Queue(message), CU(failures=10)
    alerts: list[int] = []
    processor = CleanupProcessor(Documents(), cu, WorkQueue(), lambda: NOW)
    pump = QueuePump(queue, processor.process, parse_cleanup_message,
                     retry_forever=True, alert=lambda attempt: alerts.append(attempt), renew_interval=60)
    await pump.handle(message)
    assert queue.deleted == 0
    assert queue.updates[-1] >= 4
    assert alerts == [7]
    assert cu.calls == 1
