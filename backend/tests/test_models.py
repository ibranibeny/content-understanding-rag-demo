from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.domain.models import (
    ContentResultCleanupMessage,
    DocumentRecord,
    DocumentState,
    IngestionMessage,
    OutboxRecord,
    SessionRecord,
    VersionedDocument,
)

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
DOCUMENT_ID = UUID("9f4b8484-9f6b-44f2-b4d4-e5e7687c80df")
CORRELATION_ID = UUID("868fba2c-1695-42d4-af7f-79069e434b34")


def ingestion_message(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "version": 1,
        "sessionKey": "a" * 64,
        "documentId": str(DOCUMENT_ID),
        "blobName": "uploads/a/file.pdf",
        "correlationId": str(CORRELATION_ID),
        "enqueuedAt": NOW,
    }
    values.update(overrides)
    return values


def test_queue_message_rejects_unknown_versions() -> None:
    with pytest.raises(ValidationError):
        IngestionMessage.model_validate(ingestion_message(version=2))


def test_chunking_resume_stage_parses_and_serializes_camel_case() -> None:
    message = IngestionMessage.model_validate(ingestion_message(resumeStage="chunking"))

    assert message.resume_stage == "chunking"
    assert message.model_dump(by_alias=True, mode="json")["resumeStage"] == "chunking"


def test_state_machine_includes_remote_cleanup() -> None:
    assert DocumentState.RESULT_CLEANUP_PENDING.value == "result_cleanup_pending"


def test_models_are_immutable_and_dump_camel_case() -> None:
    record = SessionRecord(
        session_key="b" * 64,
        created_at=NOW,
        expires_at=NOW,
    )

    with pytest.raises(ValidationError):
        record.document_count = 1

    dumped = record.model_dump(by_alias=True, mode="json")
    assert dumped["sessionKey"] == "b" * 64
    assert "createdAt" in dumped


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_key", "A" * 64),
        ("session_key", "a" * 63),
        ("created_at", datetime.fromisoformat("2026-09-03T10:00:00")),
    ],
)
def test_session_record_rejects_invalid_keys_and_naive_timestamps(
    field: str, value: object
) -> None:
    values: dict[str, object] = {
        "session_key": "b" * 64,
        "created_at": NOW,
        "expires_at": NOW,
    }
    values[field] = value

    with pytest.raises(ValidationError):
        SessionRecord.model_validate(values)


def test_queue_message_rejects_non_uuid_ids() -> None:
    with pytest.raises(ValidationError):
        IngestionMessage.model_validate(ingestion_message(documentId="not-a-uuid"))


def test_versioned_document_uses_value_wrapper_for_repository_consumers() -> None:
    document = DocumentRecord(
        session_key="a" * 64,
        document_id=DOCUMENT_ID,
        file_name="file.pdf",
        content_type="application/pdf",
        size_bytes=100,
        blob_name="uploads/a/file.pdf",
        state=DocumentState.AWAITING_UPLOAD,
        created_at=NOW,
        updated_at=NOW,
        expires_at=NOW,
        content_result_id="result-1",
        content_operation_url="https://example.test/operations/1",
    )

    versioned = VersionedDocument(value=document, etag='W/"1"')

    assert versioned.value.content_result_id == "result-1"


def test_outbox_kind_must_match_payload_type() -> None:
    cleanup = ContentResultCleanupMessage(
        version=1,
        session_key="a" * 64,
        document_id=DOCUMENT_ID,
        result_id="result-1",
        correlation_id=CORRELATION_ID,
        enqueued_at=NOW,
    )

    with pytest.raises(ValidationError):
        OutboxRecord(
            outbox_id="ingest:document:1",
            session_key="a" * 64,
            kind="ingestion",
            payload=cleanup,
            created_at=NOW,
        )
