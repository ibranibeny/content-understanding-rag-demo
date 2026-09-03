from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.domain.models import (
    ChatRequest,
    ChatTurn,
    Citation,
    ContentResultCleanupMessage,
    DocumentChunk,
    DocumentRecord,
    DocumentResponse,
    DocumentState,
    IngestionMessage,
    OutboxRecord,
    RetrievedEvidence,
    SessionRecord,
    SessionResponse,
    UploadCompleteRequest,
    UploadInitRequest,
    UploadInitResponse,
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
    assert message.model_dump(mode="json")["resumeStage"] == "chunking"
    assert '"resumeStage":"chunking"' in message.model_dump_json()
    assert "resume_stage" in message.model_dump(by_alias=False)


def test_queue_message_defaults_resume_stage_to_analyzing() -> None:
    message = IngestionMessage.model_validate(ingestion_message())

    assert message.resume_stage == "analyzing"
    assert message.model_dump(mode="json")["resumeStage"] == "analyzing"


def test_state_machine_has_exact_required_states() -> None:
    assert {state.value for state in DocumentState} == {
        "awaiting_upload",
        "queued",
        "analyzing",
        "classified",
        "extracted",
        "result_cleanup_pending",
        "chunking",
        "embedding",
        "indexing",
        "ready",
        "deleting",
        "deleted",
        "failed",
    }


def test_models_are_immutable_and_dump_camel_case() -> None:
    record = SessionRecord(
        session_key="b" * 64,
        created_at=NOW,
        expires_at=NOW,
    )

    with pytest.raises(ValidationError):
        record.document_count = 1

    dumped = record.model_dump(mode="json")
    assert dumped["sessionKey"] == "b" * 64
    assert "createdAt" in dumped
    assert '"sessionKey"' in record.model_dump_json()


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


@pytest.mark.parametrize("field", ["documentId", "correlationId"])
def test_queue_message_rejects_every_invalid_uuid(field: str) -> None:
    with pytest.raises(ValidationError):
        IngestionMessage.model_validate(ingestion_message(**{field: "not-a-uuid"}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sessionKey", "A" * 64),
        ("sessionKey", "a" * 63),
        ("enqueuedAt", datetime.fromisoformat("2026-09-03T10:00:00")),
        ("resumeStage", "indexing"),
    ],
)
def test_queue_message_rejects_invalid_session_time_and_stage(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        IngestionMessage.model_validate(ingestion_message(**{field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 2),
        ("sessionKey", "invalid"),
        ("documentId", "not-a-uuid"),
        ("correlationId", "not-a-uuid"),
        ("enqueuedAt", datetime.fromisoformat("2026-09-03T10:00:00")),
    ],
)
def test_cleanup_message_rejects_invalid_boundary_values(field: str, value: object) -> None:
    values: dict[str, object] = {
        "version": 1,
        "sessionKey": "a" * 64,
        "documentId": str(DOCUMENT_ID),
        "resultId": "result-1",
        "correlationId": str(CORRELATION_ID),
        "enqueuedAt": NOW,
    }
    values[field] = value

    with pytest.raises(ValidationError):
        ContentResultCleanupMessage.model_validate(values)


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


def test_all_persistence_queue_and_evidence_models_validate_and_dump_aliases() -> None:
    session = SessionRecord(session_key="a" * 64, created_at=NOW, expires_at=NOW)
    document = DocumentRecord(
        session_key="a" * 64,
        document_id=DOCUMENT_ID,
        file_name="file.pdf",
        content_type="application/pdf",
        size_bytes=100,
        blob_name="uploads/a/file.pdf",
        state=DocumentState.READY,
        created_at=NOW,
        updated_at=NOW,
        expires_at=NOW,
    )
    ingestion = IngestionMessage.model_validate(ingestion_message())
    cleanup = ContentResultCleanupMessage(
        version=1,
        session_key="a" * 64,
        document_id=DOCUMENT_ID,
        result_id="result-1",
        correlation_id=CORRELATION_ID,
        enqueued_at=NOW,
    )
    outbox = OutboxRecord(
        outbox_id="cleanup:document:1",
        session_key="a" * 64,
        kind="content_result_cleanup",
        payload=cleanup,
        created_at=NOW,
    )
    chunk = DocumentChunk(
        chunk_id="chunk-1",
        session_key="a" * 64,
        document_id=DOCUMENT_ID,
        ordinal=0,
        file_name="file.pdf",
        source_locator="page 1",
        content="Evidence",
        content_vector=(0.1, 0.2),
        expires_at=NOW,
    )
    evidence = RetrievedEvidence(
        citation_id="citation-1",
        document_id=DOCUMENT_ID,
        chunk_id="chunk-1",
        file_name="file.pdf",
        source_locator="page 1",
        content="Evidence",
    )
    citation = Citation(
        citation_id="citation-1",
        document_id=DOCUMENT_ID,
        file_name="file.pdf",
        source_locator="page 1",
    )
    versioned = VersionedDocument(value=document, etag='W/"1"')

    models = (session, document, ingestion, cleanup, outbox, chunk, evidence, citation, versioned)
    for model in models:
        assert model.model_dump_json()
        assert all("_" not in key for key in model.model_dump())

    assert chunk.model_dump()["contentVector"] == (0.1, 0.2)
    assert evidence.model_dump()["citationId"] == "citation-1"
    assert citation.model_dump()["sourceLocator"] == "page 1"


def test_all_api_dtos_validate_aliases_and_serialize_camel_case_by_default() -> None:
    dtos = (
        SessionResponse(
            expires_at=NOW,
            documents_used=1,
            document_limit=5,
            bytes_used=100,
            byte_limit=500,
            questions_used=2,
            question_limit=30,
        ),
        UploadInitRequest(file_name="file.pdf", content_type="application/pdf", size_bytes=100),
        UploadInitResponse(
            upload_url="https://example.test/upload",
            document_id=DOCUMENT_ID,
            expires_at=NOW,
            required_headers={"x-ms-blob-type": "BlockBlob"},
        ),
        UploadCompleteRequest(etag='W/"1"'),
        DocumentResponse(
            document_id=DOCUMENT_ID,
            file_name="file.pdf",
            state=DocumentState.READY,
            created_at=NOW,
            updated_at=NOW,
            expires_at=NOW,
        ),
        ChatTurn(role="user", content="What is this?"),
        ChatRequest(
            question="What is this?",
            document_ids=(DOCUMENT_ID,),
            history=(ChatTurn(role="user", content="Summarize it."),),
        ),
    )

    for dto in dtos:
        dumped = dto.model_dump()
        assert dumped
        assert all("_" not in key for key in dumped)
        assert dto.model_dump_json()

    parsed = ChatRequest.model_validate(
        {
            "question": "What is this?",
            "documentIds": [str(DOCUMENT_ID)],
            "history": [{"role": "user", "content": "Summarize it."}],
        }
    )
    assert parsed.document_ids == (DOCUMENT_ID,)
