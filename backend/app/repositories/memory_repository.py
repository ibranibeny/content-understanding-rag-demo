import asyncio
from datetime import datetime
from uuid import UUID

from app.core.errors import ConcurrencyConflict
from app.domain.models import (
    ContentResultCleanupMessage,
    DocumentRecord,
    IngestionMessage,
    OutboxRecord,
    SessionRecord,
    VersionedDocument,
)


class MemorySessionRepository:
    """Process-local session storage with opaque optimistic-concurrency versions."""

    def __init__(self) -> None:
        self._sessions: dict[str, tuple[SessionRecord, int]] = {}

    @staticmethod
    def _etag(version: int) -> str:
        return f'W/"{version}"'

    async def get(self, session_key: str) -> tuple[SessionRecord, str] | None:
        stored = self._sessions.get(session_key)
        if stored is None:
            return None
        record, version = stored
        return record, self._etag(version)

    async def create(self, session: SessionRecord) -> tuple[SessionRecord, str]:
        if session.session_key in self._sessions:
            raise ConcurrencyConflict
        self._sessions[session.session_key] = (session, 1)
        return session, self._etag(1)

    async def replace(self, session: SessionRecord, etag: str) -> tuple[SessionRecord, str]:
        stored = self._sessions.get(session.session_key)
        if stored is None:
            raise ConcurrencyConflict
        _, version = stored
        if etag != self._etag(version):
            raise ConcurrencyConflict
        next_version = version + 1
        self._sessions[session.session_key] = (session, next_version)
        return session, self._etag(next_version)


class MemoryDocumentRepository:
    """Process-local document/outbox store with transaction-like locking for tests and local use."""

    def __init__(self) -> None:
        self._documents: dict[tuple[str, str], tuple[DocumentRecord, int]] = {}
        self._outbox: dict[str, tuple[OutboxRecord, int]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _etag(version: int) -> str:
        return f'W/"{version}"'

    @staticmethod
    def _key(session_key: str, document_id: UUID) -> tuple[str, str]:
        return session_key, str(document_id)

    async def get(self, session_key: str, document_id: UUID) -> VersionedDocument | None:
        stored = self._documents.get(self._key(session_key, document_id))
        if stored is None:
            return None
        record, version = stored
        return VersionedDocument(value=record, etag=self._etag(version))

    async def create(self, document: DocumentRecord) -> VersionedDocument:
        async with self._lock:
            key = self._key(document.session_key, document.document_id)
            if key in self._documents:
                raise ConcurrencyConflict
            self._documents[key] = (document, 1)
            return VersionedDocument(value=document, etag=self._etag(1))

    async def replace(self, document: DocumentRecord, etag: str) -> VersionedDocument:
        async with self._lock:
            key = self._key(document.session_key, document.document_id)
            stored = self._documents.get(key)
            if stored is None or etag != self._etag(stored[1]):
                raise ConcurrencyConflict
            version = stored[1] + 1
            self._documents[key] = (document, version)
            return VersionedDocument(value=document, etag=self._etag(version))

    async def delete(self, session_key: str, document_id: UUID, etag: str) -> None:
        async with self._lock:
            key = self._key(session_key, document_id)
            stored = self._documents.get(key)
            if stored is None or etag != self._etag(stored[1]):
                raise ConcurrencyConflict
            del self._documents[key]

    async def list_for_session(self, session_key: str) -> list[VersionedDocument]:
        return [
            VersionedDocument(value=record, etag=self._etag(version))
            for (stored_session, _), (record, version) in self._documents.items()
            if stored_session == session_key
        ]

    async def commit_queued_with_outbox(
        self,
        document: DocumentRecord,
        document_etag: str,
        outbox: OutboxRecord,
    ) -> VersionedDocument:
        async with self._lock:
            key = self._key(document.session_key, document.document_id)
            stored = self._documents.get(key)
            if stored is None or document_etag != self._etag(stored[1]):
                raise ConcurrencyConflict
            if outbox.session_key != document.session_key or outbox.outbox_id in self._outbox:
                raise ConcurrencyConflict
            version = stored[1] + 1
            self._documents[key] = (document, version)
            self._outbox[outbox.outbox_id] = (outbox, 1)
            return VersionedDocument(value=document, etag=self._etag(version))

    async def list_pending_outbox(self, limit: int) -> list[tuple[OutboxRecord, str]]:
        pending = [
            (record, self._etag(version))
            for record, version in self._outbox.values()
            if record.sent_at is None
        ]
        pending.sort(key=lambda item: (item[0].created_at, item[0].outbox_id))
        return pending[:limit]

    async def mark_outbox_sent(self, outbox_id: str, etag: str, sent_at: datetime) -> None:
        async with self._lock:
            stored = self._outbox.get(outbox_id)
            if stored is None or etag != self._etag(stored[1]):
                raise ConcurrencyConflict
            record, version = stored
            self._outbox[outbox_id] = (
                record.model_copy(update={"sent_at": sent_at}),
                version + 1,
            )

    async def put_outbox_for_test(self, outbox: OutboxRecord) -> None:
        async with self._lock:
            if outbox.outbox_id in self._outbox:
                raise ConcurrencyConflict
            self._outbox[outbox.outbox_id] = (outbox, 1)

    async def all_outbox_for_test(self) -> list[OutboxRecord]:
        return [record for record, _ in self._outbox.values()]

    def persisted_text_for_test(self) -> str:
        documents = [record.model_dump_json() for record, _ in self._documents.values()]
        outbox = [record.model_dump_json() for record, _ in self._outbox.values()]
        return "".join((*documents, *outbox))


class MemoryWorkQueue:
    def __init__(self) -> None:
        self.ingestion_messages: list[IngestionMessage] = []
        self.cleanup_messages: list[ContentResultCleanupMessage] = []

    async def enqueue_ingestion(self, message: IngestionMessage) -> None:
        self.ingestion_messages.append(message)

    async def enqueue_result_cleanup(self, message: ContentResultCleanupMessage) -> None:
        self.cleanup_messages.append(message)