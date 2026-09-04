import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from app.domain.models import DocumentRecord, DocumentState, IngestionMessage
from app.repositories.memory_repository import MemoryDocumentRepository, MemoryWorkQueue
from app.services.content_understanding import ContentUnderstandingError
from app.services.ingestion_service import IngestionService

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
SESSION = "a" * 64
DOCUMENT = UUID("9f4b8484-9f6b-44f2-b4d4-e5e7687c80df")
CORRELATION = UUID("868fba2c-1695-42d4-af7f-79069e434b34")


class Clock:
    def now(self) -> datetime:
        return NOW


class Lease:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid

    def ensure_valid(self) -> None:
        if not self.valid:
            raise RuntimeError("lease lost")


class Blobs:
    def __init__(self, *, on_enter=None, crash_after_write: str | None = None) -> None:  # type: ignore[no-untyped-def]
        self.values: dict[str, bytes] = {}
        self.lease = Lease()
        self.on_enter = on_enter
        self.crash_after_write = crash_after_write
        self.read_urls: list[str] = []
        self.writes: list[str] = []

    @asynccontextmanager
    async def acquire_document_lease(self, session_key: str, document_id: UUID):  # type: ignore[no-untyped-def]
        if self.on_enter:
            await self.on_enter()
        yield self.lease

    async def create_read_url(self, blob_name: str, expires_at: datetime) -> str:
        self.read_urls.append(blob_name)
        return "https://blob.example/input?sig=redacted"

    async def write_derived(self, blob_name: str, data: bytes, content_type: str) -> None:
        del content_type
        self.values[blob_name] = data
        self.writes.append(blob_name)
        if self.crash_after_write == blob_name:
            raise RuntimeError("simulated crash after blob write")

    async def read_derived(self, blob_name: str) -> bytes:
        return self.values[blob_name]


class ContentUnderstanding:
    def __init__(self) -> None:
        self.start_calls: list[tuple[str, str, str | None]] = []
        self.get_calls = 0
        self.deleted_result_ids: list[str] = []
        self.delete_error: ContentUnderstandingError | None = None

    @property
    def begin_calls(self) -> int:
        return len(self.start_calls)

    async def start_analysis(  # type: ignore[no-untyped-def]
        self,
        blob_url: str,
        analyzer_id: str,
        content_range: str | None = None,
    ):
        self.start_calls.append((blob_url, analyzer_id, content_range))
        return type("Started", (), {"result_id": "result-1", "operation_url": "https://cu/op/1"})()

    async def get_result(self, operation_url: str) -> dict[str, Any]:
        del operation_url
        self.get_calls += 1
        if self.get_calls == 1:
            return {"id": "result-1", "status": "Running"}
        return {
            "id": "result-1", "status": "Succeeded", "category": "invoice",
            "markdown": "# Invoice\nTotal is 42.", "fields": {"title": "Invoice 7", "total": 42},
            "sourceLocators": {}, "pageCount": 1,
            "tokenCounts": {"tokens.gpt-5.input": 8, "tokens.gpt-5.output": 2},
        }

    async def delete_result(self, result_id: str) -> None:
        if self.delete_error:
            raise self.delete_error
        self.deleted_result_ids.append(result_id)


class Embeddings:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.25] * 3072 for _ in texts]


class Search:
    def __init__(self) -> None:
        self.chunks = []

    async def upsert(self, chunks):  # type: ignore[no-untyped-def]
        self.chunks = list(chunks)


class TrackingRepository(MemoryDocumentRepository):
    def __init__(self) -> None:
        super().__init__()
        self.states: list[str] = []

    async def replace(self, document, etag):  # type: ignore[no-untyped-def]
        previous = await self.get(document.session_key, document.document_id)
        result = await super().replace(document, etag)
        if previous is None or previous.value.state is not document.state:
            self.states.append(document.state.value)
        return result

    async def commit_document_with_outbox(self, document, document_etag, outbox):  # type: ignore[no-untyped-def]
        result = await super().commit_document_with_outbox(document, document_etag, outbox)
        self.states.append(document.state.value)
        return result


def record(**overrides: Any) -> DocumentRecord:
    values = {
        "session_key": SESSION, "document_id": DOCUMENT, "file_name": "invoice.pdf",
        "content_type": "application/pdf", "size_bytes": 10, "blob_name": "uploads/input.pdf",
        "state": DocumentState.QUEUED, "created_at": NOW, "updated_at": NOW,
        "expires_at": NOW + timedelta(hours=24),
    }
    values.update(overrides)
    return DocumentRecord(**values)


def message(resume_stage: str = "analyzing") -> IngestionMessage:
    return IngestionMessage(version=1, session_key=SESSION, document_id=DOCUMENT,
                            blob_name="uploads/input.pdf", correlation_id=CORRELATION,
                            enqueued_at=NOW, resume_stage=resume_stage)  # type: ignore[arg-type]


async def harness(document: DocumentRecord | None = None, blobs: Blobs | None = None):  # type: ignore[no-untyped-def]
    repo = TrackingRepository()
    await repo.create(document or record())
    queue = MemoryWorkQueue()
    cu = ContentUnderstanding()
    blob_store = blobs or Blobs()
    service = IngestionService(repo, blob_store, cu, Embeddings(), Search(), queue, Clock(),
                               analyzer_id="router", release_sha="release-abc", poll_delays=(0, 0))
    return service, repo, blob_store, cu, queue


async def test_happy_path_reaches_exact_states_after_result_delete() -> None:
    service, repo, blobs, cu, _ = await harness()
    await service.process(message())
    stored = await repo.get(SESSION, DOCUMENT)
    assert repo.states == ["analyzing", "classified", "extracted", "chunking", "embedding", "indexing", "ready"]
    assert stored is not None and stored.value.state is DocumentState.READY
    assert stored.value.content_result_id is None
    assert stored.value.processing_metadata["releaseSha"] == "release-abc"
    assert cu.deleted_result_ids == ["result-1"]
    assert blobs.values


async def test_pdf_analysis_uses_current_persisted_content_range() -> None:
    service, _, _, cu, _ = await harness(record(content_range="301-600"))

    await service.process(message())

    assert cu.start_calls == [
        ("https://blob.example/input?sig=redacted", "router", "301-600")
    ]


async def test_redelivery_resumes_existing_operation_without_new_analysis() -> None:
    service, repo, _, cu, _ = await harness(record(state=DocumentState.ANALYZING,
        content_range="301-600", content_result_id="result-1",
        content_operation_url="https://cu/op/1"))
    await service.process(message())
    assert cu.begin_calls == 0
    assert cu.get_calls >= 1
    stored = await repo.get(SESSION, DOCUMENT)
    assert stored is not None and stored.value.state is DocumentState.READY
    assert stored.value.content_range == "301-600"


async def test_chunking_resume_reads_normalized_blob_and_never_analyzes() -> None:
    normalized = (b'{"category":"invoice","markdown":"# Saved\\nBody","fields":{"title":"Saved"},'
                  b'"sourceLocators":{},"pageCount":1,"tokenCounts":{}}')
    path = f"derived/{SESSION}/{DOCUMENT}/normalized.json"
    blobs = Blobs()
    blobs.values[path] = normalized
    service, repo, _, cu, _ = await harness(record(state=DocumentState.CHUNKING,
        content_result_id=None, content_operation_url=None, markdown_blob_name=path), blobs)
    await service.process(message("chunking"))
    assert cu.begin_calls == cu.get_calls == 0
    assert (await repo.get(SESSION, DOCUMENT)).value.title == "Saved"  # type: ignore[union-attr]


async def test_redelivery_after_remote_delete_retries_delete_without_polling() -> None:
    normalized = (b'{"category":"invoice","markdown":"# Saved\\nBody","fields":{"title":"Saved"},'
                  b'"sourceLocators":{},"pageCount":1,"tokenCounts":{}}')
    path = f"derived/{SESSION}/{DOCUMENT}/normalized.json"
    blobs = Blobs()
    blobs.values[path] = normalized
    service, repo, _, cu, _ = await harness(record(
        state=DocumentState.EXTRACTED,
        content_result_id="result-1",
        content_operation_url="https://cu/op/1",
        markdown_blob_name=path,
    ), blobs)

    await service.process(message())

    assert cu.get_calls == 0
    assert cu.deleted_result_ids == ["result-1"]
    stored = await repo.get(SESSION, DOCUMENT)
    assert stored is not None and stored.value.state is DocumentState.READY
    assert stored.value.content_result_id is None


@pytest.mark.parametrize("crash_blob", ["normalized.json", "content.md"])
async def test_crash_after_derived_write_before_metadata_repolls_and_rewrites(
    crash_blob: str,
) -> None:
    normalized_path = f"derived/{SESSION}/{DOCUMENT}/normalized.json"
    markdown_path = f"derived/{SESSION}/{DOCUMENT}/content.md"
    crash_path = normalized_path if crash_blob == "normalized.json" else markdown_path
    blobs = Blobs(crash_after_write=crash_path)
    service, repo, _, cu, _ = await harness(blobs=blobs)

    with pytest.raises(RuntimeError, match="simulated crash after blob write"):
        await service.process(message())

    crashed = await repo.get(SESSION, DOCUMENT)
    assert crashed is not None
    assert crashed.value.content_result_id == "result-1"
    assert crashed.value.markdown_blob_name is None
    assert crashed.value.document_type is None
    assert crashed.value.extraction is None
    assert crashed.value.page_count is None
    assert "contentUnderstandingTokenCounts" not in crashed.value.processing_metadata
    assert cu.deleted_result_ids == []

    blobs.crash_after_write = None
    calls_before_redelivery = cu.get_calls
    await service.process(message())

    assert cu.get_calls > calls_before_redelivery
    assert blobs.writes.count(normalized_path) == 2
    assert blobs.writes.count(markdown_path) == (1 if crash_blob == "normalized.json" else 2)
    recovered = await repo.get(SESSION, DOCUMENT)
    assert recovered is not None and recovered.value.state is DocumentState.READY
    assert recovered.value.document_type == "invoice"
    assert recovered.value.extraction == {"title": "Invoice 7", "total": 42}
    assert recovered.value.page_count == 1
    assert recovered.value.processing_metadata["contentUnderstandingTokenCounts"] == {
        "tokens.gpt-5.input": 8,
        "tokens.gpt-5.output": 2,
    }


async def test_crash_after_metadata_before_remote_delete_resumes_from_blob() -> None:
    service, repo, blobs, cu, _ = await harness()
    original_delete = cu.delete_result
    crashed_once = False

    async def crash_before_delete(result_id: str) -> None:
        nonlocal crashed_once
        if not crashed_once:
            crashed_once = True
            raise RuntimeError("simulated crash before remote delete")
        await original_delete(result_id)

    cu.delete_result = crash_before_delete  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="simulated crash before remote delete"):
        await service.process(message())

    crashed = await repo.get(SESSION, DOCUMENT)
    assert crashed is not None
    assert crashed.value.state is DocumentState.EXTRACTED
    assert crashed.value.markdown_blob_name is not None
    assert crashed.value.markdown_blob_name in blobs.values
    calls_before_redelivery = cu.get_calls

    await service.process(message())

    assert cu.get_calls == calls_before_redelivery
    assert cu.deleted_result_ids == ["result-1"]
    recovered = await repo.get(SESSION, DOCUMENT)
    assert recovered is not None and recovered.value.state is DocumentState.READY


async def test_transient_delete_persists_cleanup_outbox_and_stops_before_chunking() -> None:
    service, repo, _, cu, queue = await harness()
    cu.delete_error = ContentUnderstandingError("unavailable", retryable=True, retry_after=7)
    await service.process(message())
    stored = await repo.get(SESSION, DOCUMENT)
    assert stored is not None and stored.value.state is DocumentState.RESULT_CLEANUP_PENDING
    assert stored.value.content_result_id == "result-1"
    assert len(queue.cleanup_messages) == 1
    assert repo.states[-1] == "result_cleanup_pending"


async def test_tombstone_before_processing_or_after_lease_is_a_noop() -> None:
    service, repo, _, cu, _ = await harness(record(state=DocumentState.DELETING, tombstoned_at=NOW))
    await service.process(message())
    assert cu.begin_calls == 0
    assert repo.states == []

    async def tombstone() -> None:
        current = await repo2.get(SESSION, DOCUMENT)
        assert current
        await repo2.replace(current.value.model_copy(update={"state": DocumentState.DELETING,
                                                              "tombstoned_at": NOW}), current.etag)

    blobs = Blobs(on_enter=tombstone)
    service2, repo2, _, cu2, _ = await harness(blobs=blobs)
    await service2.process(message())
    assert cu2.begin_calls == 0


async def test_expired_document_and_duplicate_ready_delivery_are_noops() -> None:
    service, repo, _, cu, _ = await harness(record(expires_at=NOW))
    await service.process(message())
    assert cu.begin_calls == 0 and repo.states == []
    service, repo, _, cu, _ = await harness(record(state=DocumentState.READY, chunk_count=1,
                                                     token_count=4))
    await service.process(message())
    assert cu.begin_calls == 0 and repo.states == []


async def test_failed_document_redelivery_is_terminal_and_does_not_analyze() -> None:
    service, repo, _, cu, _ = await harness(record(
        state=DocumentState.FAILED,
        failure_code="content_understanding_failed",
        retry_count=5,
    ))

    await service.process(message())

    assert cu.begin_calls == cu.get_calls == 0
    assert repo.states == []


async def test_poll_honors_retry_after_and_is_bounded() -> None:
    sleeps: list[float] = []
    service, _, _, cu, _ = await harness()
    calls = 0

    async def get_result(operation_url: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        del operation_url
        calls += 1
        if calls == 1:
            raise ContentUnderstandingError("throttled", retryable=True, retry_after=3)
        return {"id": "result-1", "status": "Succeeded", "category": "general",
                "markdown": "body", "fields": {}, "sourceLocators": {}, "pageCount": 1,
                "tokenCounts": {}}

    cu.get_result = get_result  # type: ignore[method-assign]
    service._sleep = lambda delay: _record_sleep(sleeps, delay)  # type: ignore[attr-defined]
    await service.process(message())
    assert sleeps == [3]


async def _record_sleep(values: list[float], delay: float) -> None:
    values.append(delay)
    await asyncio.sleep(0)
