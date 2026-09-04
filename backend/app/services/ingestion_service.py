from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any, Protocol, cast

from pydantic import JsonValue

from app.domain.models import (
    ContentResultCleanupMessage,
    DocumentChunk,
    DocumentRecord,
    DocumentState,
    IngestionMessage,
    OutboxRecord,
    VersionedDocument,
)
from app.domain.protocols import (
    BlobStore,
    ChunkSearch,
    Clock,
    DocumentRepository,
    EmbeddingClient,
    WorkQueue,
)
from app.services.chunking import chunk_markdown, count_tokens
from app.services.content_understanding import ContentUnderstandingError

POLL_DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 30.0, 30.0)


class ContentClient(Protocol):
    async def start_analysis(
        self,
        blob_url: str,
        analyzer_id: str,
        content_range: str | None = None,
    ) -> Any: ...
    async def get_result(self, operation_url: str) -> Mapping[str, Any]: ...
    async def delete_result(self, result_id: str) -> None: ...


class IngestionService:
    def __init__(
        self,
        documents: DocumentRepository,
        blobs: BlobStore,
        content: ContentClient,
        embeddings: EmbeddingClient,
        search: ChunkSearch,
        queue: WorkQueue,
        clock: Clock,
        *,
        analyzer_id: str,
        release_sha: str,
        poll_delays: Sequence[float] = POLL_DELAYS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._documents = documents
        self._blobs = blobs
        self._content = content
        self._embeddings = embeddings
        self._search = search
        self._queue = queue
        self._clock = clock
        self._analyzer_id = analyzer_id
        self._release_sha = release_sha
        self._poll_delays = tuple(poll_delays)
        self._sleep = sleep

    async def process(self, message: IngestionMessage) -> None:
        current = await self._documents.get(message.session_key, message.document_id)
        if not self._processable(current):
            return
        assert current is not None
        if current.value.blob_name != message.blob_name:
            return
        async with self._blobs.acquire_document_lease(message.session_key, message.document_id) as lease:
            current = await self._documents.get(message.session_key, message.document_id)
            if not self._processable(current):
                return
            assert current is not None
            lease.ensure_valid()
            normalized = await self._analysis_or_resume(current, message, lease)
            if normalized is None:
                return
            await self._index(current=await self._required(message), normalized=normalized, lease=lease)

    def _processable(self, current: VersionedDocument | None) -> bool:
        if current is None:
            return False
        document = current.value
        return (
            document.state not in {
                DocumentState.READY,
                DocumentState.RESULT_CLEANUP_PENDING,
                DocumentState.DELETING,
                DocumentState.DELETED,
                DocumentState.FAILED,
            }
            and document.tombstoned_at is None
            and document.expires_at > self._clock.now()
        )

    async def _required(self, message: IngestionMessage) -> VersionedDocument:
        current = await self._documents.get(message.session_key, message.document_id)
        if not self._processable(current):
            raise RuntimeError("document lifecycle fence changed")
        assert current is not None
        return current

    async def _transition(self, current: VersionedDocument, state: DocumentState, **changes: Any) -> VersionedDocument:
        document = current.value.model_copy(update={
            "state": state,
            "updated_at": self._clock.now(),
            "processing_metadata": {**current.value.processing_metadata, "releaseSha": self._release_sha},
            **changes,
        })
        return await self._documents.replace(document, current.etag)

    async def _analysis_or_resume(self, current: VersionedDocument, message: IngestionMessage, lease: Any) -> Mapping[str, Any] | None:
        document = current.value
        normalized_path = document.markdown_blob_name or self._normalized_path(document)
        if message.resume_stage == "chunking" or (
            document.content_result_id is None and document.markdown_blob_name is not None
        ):
            return self._decode_normalized(await self._blobs.read_derived(normalized_path))

        if document.markdown_blob_name is not None and document.content_result_id is not None:
            normalized = self._decode_normalized(await self._blobs.read_derived(normalized_path))
            return await self._delete_result_or_defer(current, message, lease, normalized)

        if document.content_result_id is None or document.content_operation_url is None:
            current = await self._transition(current, DocumentState.ANALYZING)
            lease.ensure_valid()
            read_url = await self._blobs.create_read_url(document.blob_name or message.blob_name,
                                                         self._clock.now() + timedelta(minutes=15))
            started = await self._content.start_analysis(
                read_url,
                self._analyzer_id,
                document.content_range,
            )
            current = await self._transition(
                current, DocumentState.ANALYZING,
                content_result_id=str(started.result_id),
                content_operation_url=str(started.operation_url),
            )

        operation_url = current.value.content_operation_url
        result_id = current.value.content_result_id
        assert operation_url is not None and result_id is not None
        normalized = await self._poll(operation_url, lease)
        current = await self._transition(current, DocumentState.CLASSIFIED)
        fields = cast(Mapping[str, JsonValue], normalized["fields"])
        title = fields.get("title")
        await self._blobs.write_derived(
            normalized_path,
            self._encode_normalized(normalized),
            "application/json",
        )
        markdown_path = self._markdown_path(current.value)
        await self._blobs.write_derived(
            markdown_path,
            str(normalized["markdown"]).encode(),
            "text/markdown; charset=utf-8",
        )
        lease.ensure_valid()
        token_counts = normalized.get("tokenCounts")
        processing_metadata = dict(current.value.processing_metadata)
        if isinstance(token_counts, Mapping):
            processing_metadata["contentUnderstandingTokenCounts"] = cast(
                JsonValue,
                dict(token_counts),
            )
        current = await self._transition(
            current, DocumentState.EXTRACTED,
            document_type=str(normalized["category"]),
            extraction=cast(JsonValue, dict(fields)),
            title=title if isinstance(title, str) else current.value.title,
            page_count=int(normalized.get("pageCount", 0)),
            markdown_blob_name=normalized_path,
            processing_metadata=processing_metadata,
        )
        return await self._delete_result_or_defer(current, message, lease, normalized)

    async def _delete_result_or_defer(
        self,
        current: VersionedDocument,
        message: IngestionMessage,
        lease: Any,
        normalized: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        result_id = current.value.content_result_id
        assert result_id is not None
        lease.ensure_valid()
        try:
            await self._content.delete_result(result_id)
        except ContentUnderstandingError as error:
            if not error.retryable:
                raise
            cleanup = ContentResultCleanupMessage(
                version=1, session_key=message.session_key, document_id=message.document_id,
                result_id=result_id, correlation_id=message.correlation_id, enqueued_at=self._clock.now(),
            )
            pending = current.value.model_copy(update={
                "state": DocumentState.RESULT_CLEANUP_PENDING,
                "updated_at": self._clock.now(),
            })
            outbox = OutboxRecord(
                outbox_id=f"cu-cleanup:{message.document_id}:{result_id}",
                session_key=message.session_key, kind="content_result_cleanup",
                payload=cleanup, created_at=self._clock.now(),
            )
            await self._documents.commit_document_with_outbox(pending, current.etag, outbox)
            await self._queue.enqueue_result_cleanup(cleanup)
            return None
        await self._transition(current, DocumentState.CHUNKING,
                               content_result_id=None, content_operation_url=None)
        return normalized

    async def _poll(self, operation_url: str, lease: Any) -> Mapping[str, Any]:
        for index in range(len(self._poll_delays) + 1):
            lease.ensure_valid()
            try:
                result = await self._content.get_result(operation_url)
            except ContentUnderstandingError as error:
                if not error.retryable or index == len(self._poll_delays):
                    raise
                await self._sleep(error.retry_after if error.retry_after is not None else self._poll_delays[index])
                continue
            if result.get("status") == "Succeeded":
                return result
            if result.get("status") not in {"NotStarted", "Running"}:
                raise ContentUnderstandingError("content_understanding_failed", retryable=False)
            if index == len(self._poll_delays):
                break
            await self._sleep(self._poll_delays[index])
        raise ContentUnderstandingError("content_understanding_poll_timeout", retryable=True)

    async def _index(self, *, current: VersionedDocument, normalized: Mapping[str, Any], lease: Any) -> None:
        fields = normalized.get("fields")
        fields = fields if isinstance(fields, dict) else {}
        title = fields.get("title")
        metadata = {
            "document_type": str(normalized.get("category", current.value.document_type)),
            "extraction": cast(JsonValue, fields),
            "title": title if isinstance(title, str) else current.value.title,
            "page_count": int(normalized.get("pageCount", current.value.page_count or 0)),
        }
        if current.value.state is not DocumentState.CHUNKING:
            current = await self._transition(
                current,
                DocumentState.CHUNKING,
                content_result_id=None,
                content_operation_url=None,
                **metadata,
            )
        elif any(getattr(current.value, key) != value for key, value in metadata.items()):
            current = await self._transition(current, DocumentState.CHUNKING, **metadata)
        markdown = str(normalized["markdown"])
        drafts = chunk_markdown(markdown, document_id=current.value.document_id)
        lease.ensure_valid()
        current = await self._transition(current, DocumentState.EMBEDDING)
        vectors = await self._embeddings.embed([draft.content for draft in drafts])
        if len(vectors) != len(drafts):
            raise RuntimeError("embedding count mismatch")
        lease.ensure_valid()
        current = await self._transition(current, DocumentState.INDEXING)
        chunks = [DocumentChunk(
            chunk_id=draft.chunk_id, session_key=current.value.session_key,
            document_id=current.value.document_id, ordinal=draft.ordinal,
            file_name=current.value.file_name or "document", document_type=current.value.document_type,
            title=current.value.title, section_path=draft.section_path, page_number=draft.page_number,
            source_locator=draft.source_locator, content=draft.content,
            content_vector=tuple(vector), expires_at=current.value.expires_at,
        ) for draft, vector in zip(drafts, vectors, strict=True)]
        await self._search.upsert(chunks)
        lease.ensure_valid()
        await self._transition(current, DocumentState.READY,
                               chunk_count=len(chunks), token_count=count_tokens(markdown))

    @staticmethod
    def _normalized_path(document: DocumentRecord) -> str:
        return f"derived/{document.session_key}/{document.document_id}/normalized.json"

    @staticmethod
    def _markdown_path(document: DocumentRecord) -> str:
        return f"derived/{document.session_key}/{document.document_id}/content.md"

    @staticmethod
    def _encode_normalized(normalized: Mapping[str, Any]) -> bytes:
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()

    @staticmethod
    def _decode_normalized(data: bytes) -> Mapping[str, Any]:
        value = json.loads(data)
        if not isinstance(value, dict) or not isinstance(value.get("markdown"), str):
            raise TypeError("invalid normalized result")
        return value