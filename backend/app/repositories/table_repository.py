from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, cast
from uuid import UUID

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import UpdateMode
from azure.data.tables.aio import TableClient
from azure.identity.aio import DefaultAzureCredential
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import ConcurrencyConflict, RepositoryDataError
from app.domain.models import (
    DocumentRecord,
    DocumentState,
    OutboxRecord,
    SessionRecord,
    VersionedDocument,
)

CODEC_VERSION = 1
SESSION_ROW_KEY = "session"
DOCUMENT_ROW_PREFIX = "document:"
OUTBOX_ROW_PREFIX = "outbox:"

Entity = Mapping[str, object]
TransactionOperation = tuple[Any, ...]


class TableClientLike(Protocol):
    async def get_entity(self, *, partition_key: str, row_key: str) -> Entity: ...

    async def create_entity(self, entity: Entity) -> Entity: ...

    async def update_entity(
        self, entity: Entity, *, mode: object, etag: str, match_condition: object
    ) -> Entity: ...

    async def delete_entity(
        self, *, partition_key: str, row_key: str, etag: str, match_condition: object
    ) -> None: ...

    def query_entities(
        self, query_filter: str, *, parameters: Mapping[str, object] | None = None
    ) -> Any: ...

    async def submit_transaction(self, operations: list[TransactionOperation]) -> list[Entity]: ...

    async def close(self) -> None: ...


def _partition(session_key: str) -> str:
    return f"session:{session_key}"


def _etag(entity_or_response: object) -> str:
    metadata = getattr(entity_or_response, "metadata", None)
    if isinstance(metadata, Mapping) and isinstance(metadata.get("etag"), str):
        return cast(str, metadata["etag"])
    if isinstance(entity_or_response, Mapping):
        value = entity_or_response.get("etag") or entity_or_response.get("ETag")
        if isinstance(value, str):
            return value
    raise RepositoryDataError


def _entity(record: SessionRecord | DocumentRecord | OutboxRecord, row_key: str) -> dict[str, object]:
    payload = record.model_dump_json()
    result: dict[str, object] = {
        "PartitionKey": _partition(record.session_key),
        "RowKey": row_key,
        "CodecVersion": CODEC_VERSION,
        "EntityType": type(record).__name__,
        "Payload": payload,
    }
    if isinstance(record, OutboxRecord):
        result["Sent"] = record.sent_at is not None
        result["CreatedAt"] = record.created_at.isoformat().replace("+00:00", "Z")
    if isinstance(record, DocumentRecord):
        result["State"] = record.state.value
        result["ExpiresAt"] = record.expires_at.isoformat().replace("+00:00", "Z")
        result["UpdatedAt"] = record.updated_at.isoformat().replace("+00:00", "Z")
        if record.deleted_at is not None:
            result["DeletedAt"] = record.deleted_at.isoformat().replace("+00:00", "Z")
    return result


def _decode(entity: Entity, model: type[SessionRecord | DocumentRecord | OutboxRecord]) -> Any:
    try:
        if entity.get("CodecVersion") != CODEC_VERSION:
            raise ValueError
        expected_type = model.__name__
        if entity.get("EntityType") != expected_type:
            raise ValueError
        payload = entity.get("Payload")
        if not isinstance(payload, str):
            raise TypeError
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise TypeError
        return model.model_validate(raw)
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError):
        raise RepositoryDataError from None


async def _all_pages(pager: Any) -> list[Entity]:
    entities: list[Entity] = []
    async for page in pager.by_page():
        async for entity in page:
            entities.append(cast(Entity, entity))
    return entities


class TableApplicationRepository:
    """Session repository and atomic application persistence over one Azure Table."""

    def __init__(self, client: TableClientLike, *, owns_client: bool = False) -> None:
        self._client = client
        self._owns_client = owns_client
        self._closed = False
        self.documents = TableDocumentRepository(client)

    @classmethod
    def from_settings(cls, settings: Settings) -> TableApplicationRepository:
        if settings.azurite_table_connection_string:
            client = TableClient.from_connection_string(
                settings.azurite_table_connection_string, settings.table_name
            )
        else:
            endpoint = f"https://{settings.storage_account_name}.table.core.windows.net"
            client = TableClient(
                endpoint=endpoint,
                table_name=settings.table_name,
                credential=DefaultAzureCredential(),
            )
        return cls(cast(TableClientLike, client), owns_client=True)

    async def get(self, session_key: str) -> tuple[SessionRecord, str] | None:
        try:
            entity = await self._client.get_entity(
                partition_key=_partition(session_key), row_key=SESSION_ROW_KEY
            )
        except ResourceNotFoundError:
            return None
        return cast(SessionRecord, _decode(entity, SessionRecord)), _etag(entity)

    async def create(self, session: SessionRecord) -> tuple[SessionRecord, str]:
        try:
            response = await self._client.create_entity(_entity(session, SESSION_ROW_KEY))
        except (ResourceExistsError, ResourceModifiedError, ResourceNotFoundError):
            raise ConcurrencyConflict from None
        return session, _etag(response)

    async def replace(self, session: SessionRecord, etag: str) -> tuple[SessionRecord, str]:
        try:
            response = await self._client.update_entity(
                _entity(session, SESSION_ROW_KEY),
                mode=UpdateMode.REPLACE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except (ResourceExistsError, ResourceModifiedError, ResourceNotFoundError):
            raise ConcurrencyConflict from None
        return session, _etag(response)

    async def reserve_and_create(
        self, session_update: SessionRecord, session_etag: str, document: DocumentRecord
    ) -> tuple[SessionRecord, VersionedDocument]:
        if session_update.session_key != document.session_key:
            raise ConcurrencyConflict
        operations: list[TransactionOperation] = [
            (
                "update",
                _entity(session_update, SESSION_ROW_KEY),
                {
                    "mode": UpdateMode.REPLACE,
                    "etag": session_etag,
                    "match_condition": MatchConditions.IfNotModified,
                },
            ),
            ("create", _entity(document, f"{DOCUMENT_ROW_PREFIX}{document.document_id}")),
        ]
        try:
            responses = await self._client.submit_transaction(operations)
        except (ResourceExistsError, ResourceModifiedError, ResourceNotFoundError):
            raise ConcurrencyConflict from None
        return session_update, VersionedDocument(value=document, etag=_etag(responses[1]))

    async def aclose(self) -> None:
        if self._owns_client and not self._closed:
            self._closed = True
            await self._client.close()


class TableDocumentRepository:
    """Document and transactional outbox persistence over the shared Table client."""

    def __init__(self, client: TableClientLike) -> None:
        self._client = client

    async def get(self, session_key: str, document_id: UUID) -> VersionedDocument | None:
        try:
            entity = await self._client.get_entity(
                partition_key=_partition(session_key),
                row_key=f"{DOCUMENT_ROW_PREFIX}{document_id}",
            )
        except ResourceNotFoundError:
            return None
        return VersionedDocument(value=_decode(entity, DocumentRecord), etag=_etag(entity))

    async def create(self, document: DocumentRecord) -> VersionedDocument:
        try:
            response = await self._client.create_entity(
                _entity(document, f"{DOCUMENT_ROW_PREFIX}{document.document_id}")
            )
        except (ResourceExistsError, ResourceModifiedError, ResourceNotFoundError):
            raise ConcurrencyConflict from None
        return VersionedDocument(value=document, etag=_etag(response))

    async def replace(self, document: DocumentRecord, etag: str) -> VersionedDocument:
        try:
            response = await self._client.update_entity(
                _entity(document, f"{DOCUMENT_ROW_PREFIX}{document.document_id}"),
                mode=UpdateMode.REPLACE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except (ResourceExistsError, ResourceModifiedError, ResourceNotFoundError):
            raise ConcurrencyConflict from None
        return VersionedDocument(value=document, etag=_etag(response))

    async def delete(self, session_key: str, document_id: UUID, etag: str) -> None:
        try:
            await self._client.delete_entity(
                partition_key=_partition(session_key),
                row_key=f"{DOCUMENT_ROW_PREFIX}{document_id}",
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except (ResourceExistsError, ResourceModifiedError, ResourceNotFoundError):
            raise ConcurrencyConflict from None

    async def list_for_session(self, session_key: str) -> list[VersionedDocument]:
        pager = self._client.query_entities(
            "PartitionKey eq @partition and RowKey ge @start and RowKey lt @end",
            parameters={
                "partition": _partition(session_key),
                "start": DOCUMENT_ROW_PREFIX,
                "end": "documenu",
            },
        )
        entities = await _all_pages(pager)
        documents = [
            VersionedDocument(value=_decode(entity, DocumentRecord), etag=_etag(entity))
            for entity in entities
            if str(entity.get("RowKey", "")).startswith(DOCUMENT_ROW_PREFIX)
        ]
        documents.sort(key=lambda item: (item.value.created_at, str(item.value.document_id)))
        return documents

    async def list_lifecycle_candidates(
        self, now: datetime, limit: int
    ) -> list[VersionedDocument]:
        pager = self._client.query_entities(
            "RowKey ge @start and RowKey lt @end and "
            "(State eq @deleting or (State ne @deleted and ExpiresAt le @now))",
            parameters={
                "start": DOCUMENT_ROW_PREFIX,
                "end": "documenu",
                "deleting": DocumentState.DELETING.value,
                "deleted": DocumentState.DELETED.value,
                "now": now.isoformat().replace("+00:00", "Z"),
            },
        )
        decoded = [
            VersionedDocument(value=_decode(entity, DocumentRecord), etag=_etag(entity))
            for entity in await _all_pages(pager)
            if str(entity.get("RowKey", "")).startswith(DOCUMENT_ROW_PREFIX)
        ]
        documents = [
            item
            for item in decoded
            if item.value.state is DocumentState.DELETING
            or (item.value.state is not DocumentState.DELETED and item.value.expires_at <= now)
        ]
        documents.sort(key=lambda item: (item.value.updated_at, str(item.value.document_id)))
        return documents[:limit]

    async def list_deleted_before(
        self, cutoff: datetime, limit: int
    ) -> list[VersionedDocument]:
        pager = self._client.query_entities(
            "RowKey ge @start and RowKey lt @end and State eq @deleted and DeletedAt le @cutoff",
            parameters={
                "start": DOCUMENT_ROW_PREFIX,
                "end": "documenu",
                "deleted": DocumentState.DELETED.value,
                "cutoff": cutoff.isoformat().replace("+00:00", "Z"),
            },
        )
        decoded = [
            VersionedDocument(value=_decode(entity, DocumentRecord), etag=_etag(entity))
            for entity in await _all_pages(pager)
            if str(entity.get("RowKey", "")).startswith(DOCUMENT_ROW_PREFIX)
        ]
        documents = [
            item
            for item in decoded
            if item.value.state is DocumentState.DELETED
            and item.value.deleted_at is not None
            and item.value.deleted_at <= cutoff
        ]
        documents.sort(key=lambda item: (item.value.deleted_at, str(item.value.document_id)))
        return documents[:limit]

    async def commit_queued_with_outbox(
        self, document: DocumentRecord, document_etag: str, outbox: OutboxRecord
    ) -> VersionedDocument:
        if document.session_key != outbox.session_key:
            raise ConcurrencyConflict
        operations: list[TransactionOperation] = [
            (
                "update",
                _entity(document, f"{DOCUMENT_ROW_PREFIX}{document.document_id}"),
                {
                    "mode": UpdateMode.REPLACE,
                    "etag": document_etag,
                    "match_condition": MatchConditions.IfNotModified,
                },
            ),
            ("create", _entity(outbox, f"{OUTBOX_ROW_PREFIX}{outbox.outbox_id}")),
        ]
        try:
            responses = await self._client.submit_transaction(operations)
        except (ResourceExistsError, ResourceModifiedError, ResourceNotFoundError):
            raise ConcurrencyConflict from None
        return VersionedDocument(value=document, etag=_etag(responses[0]))

    async def list_pending_outbox(self, limit: int) -> list[tuple[OutboxRecord, str]]:
        pager = self._client.query_entities(
            "RowKey ge @start and RowKey lt @end and Sent eq @pending",
            parameters={"start": OUTBOX_ROW_PREFIX, "end": "outboy", "pending": False},
        )
        entities = await _all_pages(pager)
        pending = [
            (cast(OutboxRecord, _decode(entity, OutboxRecord)), _etag(entity))
            for entity in entities
            if str(entity.get("RowKey", "")).startswith(OUTBOX_ROW_PREFIX)
            and entity.get("Sent") is False
        ]
        pending.sort(key=lambda item: (item[0].created_at, item[0].outbox_id))
        return pending[:limit]

    async def get_pending_outbox(self, outbox_id: str) -> tuple[OutboxRecord, str] | None:
        pager = self._client.query_entities(
            "RowKey eq @row and Sent eq @pending",
            parameters={"row": f"{OUTBOX_ROW_PREFIX}{outbox_id}", "pending": False},
        )
        entities = await _all_pages(pager)
        if not entities:
            return None
        entity = entities[0]
        return cast(OutboxRecord, _decode(entity, OutboxRecord)), _etag(entity)

    async def mark_outbox_sent(self, outbox_id: str, etag: str, sent_at: datetime) -> None:
        pending = await self.get_pending_outbox(outbox_id)
        if pending is None or pending[1] != etag:
            raise ConcurrencyConflict
        record = pending[0].model_copy(update={"sent_at": sent_at})
        try:
            await self._client.update_entity(
                _entity(record, f"{OUTBOX_ROW_PREFIX}{outbox_id}"),
                mode=UpdateMode.REPLACE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except (ResourceExistsError, ResourceModifiedError, ResourceNotFoundError):
            raise ConcurrencyConflict from None

    async def put_outbox_for_test(self, outbox: OutboxRecord) -> None:
        try:
            await self._client.create_entity(_entity(outbox, f"{OUTBOX_ROW_PREFIX}{outbox.outbox_id}"))
        except (ResourceExistsError, ResourceModifiedError, ResourceNotFoundError):
            raise ConcurrencyConflict from None