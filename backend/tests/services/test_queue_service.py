import json
from datetime import UTC, datetime
from uuid import UUID

from app.domain.models import ContentResultCleanupMessage, IngestionMessage
from app.services.queue_service import AzureWorkQueue

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
SESSION = "a" * 64
DOCUMENT = UUID("9f4b8484-9f6b-44f2-b4d4-e5e7687c80df")
CORRELATION = UUID("868fba2c-1695-42d4-af7f-79069e434b34")


class QueueClient:
    def __init__(self, count: int = 0) -> None:
        self.messages: list[str] = []
        self.count = count
        self.close_calls = 0

    async def send_message(self, content: str) -> None:
        self.messages.append(content)

    async def get_queue_properties(self):  # type: ignore[no-untyped-def]
        return type("Properties", (), {"approximate_message_count": self.count})()

    async def close(self) -> None:
        self.close_calls += 1


async def test_queue_uses_camel_case_json_and_reports_ingestion_backlog() -> None:
    ingestion = QueueClient(7)
    cleanup = QueueClient()
    queue = AzureWorkQueue(ingestion, cleanup)
    message = IngestionMessage(
        version=1,
        session_key=SESSION,
        document_id=DOCUMENT,
        blob_name="uploads/path.pdf",
        correlation_id=CORRELATION,
        enqueued_at=NOW,
    )

    await queue.enqueue_ingestion(message)

    assert json.loads(ingestion.messages[0]) == message.model_dump(mode="json")
    assert "sessionKey" in ingestion.messages[0]
    assert "session_key" not in ingestion.messages[0]
    assert await queue.get_ingestion_backlog() == 7


async def test_queue_routes_cleanup_separately_and_closes_owned_clients_once() -> None:
    ingestion = QueueClient()
    cleanup = QueueClient()
    queue = AzureWorkQueue(ingestion, cleanup, owns_clients=True)
    message = ContentResultCleanupMessage(
        version=1,
        session_key=SESSION,
        document_id=DOCUMENT,
        result_id="result-1",
        correlation_id=CORRELATION,
        enqueued_at=NOW,
    )

    await queue.enqueue_result_cleanup(message)
    await queue.aclose()
    await queue.aclose()

    assert ingestion.messages == []
    assert json.loads(cleanup.messages[0]) == message.model_dump(mode="json")
    assert ingestion.close_calls == cleanup.close_calls == 1