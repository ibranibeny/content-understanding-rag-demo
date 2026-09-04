from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must use UTC")
    return value.astimezone(UTC)


def _require_azure_etag(value: str) -> str:
    opaque = value.removeprefix("W/")
    if (
        len(value) > 256
        or len(opaque) < 3
        or not opaque.startswith('"')
        or not opaque.endswith('"')
        or '"' in opaque[1:-1]
        or any(character.isspace() or not character.isprintable() for character in opaque[1:-1])
    ):
        raise ValueError("invalid Azure ETag")
    return value


SessionKey = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
UtcDateTime = Annotated[datetime, AfterValidator(_require_utc)]
AzureEtag = Annotated[str, StringConstraints(min_length=3), AfterValidator(_require_azure_etag)]


class ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        revalidate_instances="always",
        serialize_by_alias=True,
    )


class DocumentState(StrEnum):
    AWAITING_UPLOAD = "awaiting_upload"
    QUEUED = "queued"
    ANALYZING = "analyzing"
    CLASSIFIED = "classified"
    EXTRACTED = "extracted"
    RESULT_CLEANUP_PENDING = "result_cleanup_pending"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    READY = "ready"
    DELETING = "deleting"
    DELETED = "deleted"
    FAILED = "failed"


class SessionRecord(ContractModel):
    session_key: SessionKey
    created_at: UtcDateTime
    expires_at: UtcDateTime
    document_count: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)
    question_timestamps: tuple[UtcDateTime, ...] = ()


class DocumentRecord(ContractModel):
    session_key: SessionKey
    document_id: UUID
    file_name: str | None
    content_type: str | None
    content_range: str | None = None
    size_bytes: int | None = Field(ge=0)
    blob_name: str | None
    state: DocumentState
    created_at: UtcDateTime
    updated_at: UtcDateTime
    expires_at: UtcDateTime
    document_type: str | None = None
    title: str | None = None
    content_result_id: str | None = None
    content_operation_url: str | None = None
    extraction: JsonValue | None = None
    markdown_blob_name: str | None = None
    page_count: int | None = Field(default=None, ge=0)
    chunk_count: int | None = Field(default=None, ge=0)
    token_count: int | None = Field(default=None, ge=0)
    failure_code: str | None = None
    failure_retryable: bool = False
    retry_count: int = Field(default=0, ge=0)
    processing_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    tombstoned_at: UtcDateTime | None = None
    deletion_requested_at: UtcDateTime | None = None
    deleted_at: UtcDateTime | None = None

    @model_validator(mode="after")
    def require_active_document_metadata(self) -> Self:
        metadata = (self.file_name, self.content_type, self.size_bytes, self.blob_name)
        if self.state is DocumentState.DELETED:
            if any(value is not None for value in metadata):
                raise ValueError("deleted documents must not retain upload metadata")
        elif any(value is None for value in metadata):
            raise ValueError("active documents require upload metadata")
        return self


class IngestionMessage(ContractModel):
    version: Literal[1]
    session_key: SessionKey
    document_id: UUID
    blob_name: str
    correlation_id: UUID
    enqueued_at: UtcDateTime
    resume_stage: Literal["analyzing", "chunking"] = "analyzing"


class ContentResultCleanupMessage(ContractModel):
    version: Literal[1]
    session_key: SessionKey
    document_id: UUID
    result_id: str
    correlation_id: UUID
    enqueued_at: UtcDateTime


class OutboxRecord(ContractModel):
    outbox_id: str
    session_key: SessionKey
    kind: Literal["ingestion", "content_result_cleanup"]
    payload: IngestionMessage | ContentResultCleanupMessage
    created_at: UtcDateTime
    sent_at: UtcDateTime | None = None

    @model_validator(mode="after")
    def require_matching_payload(self) -> Self:
        valid = (
            self.kind == "ingestion" and isinstance(self.payload, IngestionMessage)
        ) or (
            self.kind == "content_result_cleanup"
            and isinstance(self.payload, ContentResultCleanupMessage)
        )
        if not valid:
            raise ValueError("outbox kind does not match payload type")
        return self


class DocumentChunk(ContractModel):
    chunk_id: str
    session_key: SessionKey
    document_id: UUID
    ordinal: int = Field(ge=0)
    file_name: str
    document_type: str | None = None
    title: str | None = None
    section_path: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    source_locator: str
    content: str
    content_vector: tuple[float, ...]
    expires_at: UtcDateTime


class RetrievedEvidence(ContractModel):
    citation_id: str
    document_id: UUID
    chunk_id: str
    file_name: str
    source_locator: str
    content: str
    search_score: float | None = None
    reranker_score: float | None = None


class Citation(ContractModel):
    citation_id: str
    document_id: UUID
    file_name: str
    source_locator: str


class VersionedDocument(ContractModel):
    value: DocumentRecord
    etag: str


class SessionResponse(ContractModel):
    expires_at: UtcDateTime
    documents_used: int = Field(ge=0)
    document_limit: int = Field(ge=0)
    bytes_used: int = Field(ge=0)
    byte_limit: int = Field(ge=0)
    questions_used: int = Field(ge=0)
    question_limit: int = Field(ge=0)


class UploadInitRequest(ContractModel):
    file_name: str
    content_type: str
    size_bytes: int = Field(gt=0)
    content_range: str | None = None


class UploadInitResponse(ContractModel):
    upload_url: str
    document_id: UUID
    expires_at: UtcDateTime
    required_headers: dict[str, str]


class UploadCompleteRequest(ContractModel):
    etag: AzureEtag


class DocumentResponse(ContractModel):
    document_id: UUID
    file_name: str
    state: DocumentState
    content_range: str | None = Field(default=None, exclude_if=lambda value: value is None)
    document_type: str | None = None
    title: str | None = None
    page_count: int | None = None
    chunk_count: int | None = None
    token_count: int | None = None
    extraction: JsonValue | None = None
    failure_code: str | None = None
    failure_retryable: bool = False
    retry_count: int = Field(default=0, ge=0)
    created_at: UtcDateTime
    updated_at: UtcDateTime
    expires_at: UtcDateTime


class DocumentSummaryResponse(ContractModel):
    document_id: UUID
    file_name: str
    state: DocumentState
    content_range: str | None = Field(default=None, exclude_if=lambda value: value is None)
    document_type: str | None = None
    title: str | None = None
    page_count: int | None = None
    chunk_count: int | None = None
    token_count: int | None = None
    failure_code: str | None = None
    failure_retryable: bool = False
    retry_count: int = Field(default=0, ge=0)
    created_at: UtcDateTime
    updated_at: UtcDateTime
    expires_at: UtcDateTime


class DocumentDeleteResponse(ContractModel):
    document_id: UUID
    state: Literal[DocumentState.DELETING, DocumentState.DELETED]
    updated_at: UtcDateTime


class ChatTurn(ContractModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(ContractModel):
    question: str = Field(min_length=1, max_length=4000)
    document_ids: tuple[UUID, ...] = ()
    history: tuple[ChatTurn, ...] = ()
