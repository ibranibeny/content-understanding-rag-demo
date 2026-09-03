from typing import Any, Protocol, cast

from azure.core.credentials_async import AsyncTokenCredential
from azure.storage.queue.aio import QueueClient

from app.core.config import Settings
from app.domain.models import ContentResultCleanupMessage, IngestionMessage


class QueueClientLike(Protocol):
    async def send_message(self, content: str) -> object: ...

    async def get_queue_properties(self) -> Any: ...

    async def close(self) -> None: ...


class AzureWorkQueue:
    """Storage Queue adapter for versioned camel-case application messages."""

    def __init__(
        self,
        ingestion: QueueClientLike,
        cleanup: QueueClientLike,
        *,
        owns_clients: bool = False,
    ) -> None:
        self._ingestion = ingestion
        self._cleanup = cleanup
        self._owns_clients = owns_clients
        self._closed = False

    @classmethod
    def from_settings(
        cls, settings: Settings, credential: AsyncTokenCredential
    ) -> "AzureWorkQueue":
        endpoint = f"https://{settings.storage_account_name}.queue.core.windows.net"
        ingestion = QueueClient(
            account_url=endpoint,
            queue_name=settings.ingestion_queue,
            credential=credential,
        )
        cleanup = QueueClient(
            account_url=endpoint,
            queue_name=settings.content_result_cleanup_queue,
            credential=credential,
        )
        return cls(
            cast(QueueClientLike, ingestion),
            cast(QueueClientLike, cleanup),
            owns_clients=True,
        )

    async def enqueue_ingestion(self, message: IngestionMessage) -> None:
        await self._ingestion.send_message(message.model_dump_json())

    async def enqueue_result_cleanup(self, message: ContentResultCleanupMessage) -> None:
        await self._cleanup.send_message(message.model_dump_json())

    async def get_ingestion_backlog(self) -> int:
        properties = await self._ingestion.get_queue_properties()
        return int(properties.approximate_message_count or 0)

    async def aclose(self) -> None:
        if self._owns_clients and not self._closed:
            self._closed = True
            await self._ingestion.close()
            await self._cleanup.close()