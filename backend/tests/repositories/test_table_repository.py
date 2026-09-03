from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from app.core.errors import ConcurrencyConflict, RepositoryDataError
from app.domain.models import (
    DocumentRecord,
    DocumentState,
    IngestionMessage,
    OutboxRecord,
    SessionRecord,
)
from app.repositories.table_repository import TableApplicationRepository

SESSION_KEY = "a" * 64
OTHER_SESSION_KEY = "b" * 64
DOCUMENT_ID = UUID("9f4b8484-9f6b-44f2-b4d4-e5e7687c80df")
NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)


class FakeEntity(dict[str, object]):
    def __init__(self, values: Mapping[str, object], etag: str) -> None:
        super().__init__(values)
        self.metadata = {"etag": etag}


class FakePages:
    def __init__(self, entities: list[FakeEntity], page_size: int = 1) -> None:
        self._entities = entities
        self._page_size = page_size

    def by_page(self, continuation_token: str | None = None) -> AsyncIterator[AsyncIterator[FakeEntity]]:
        del continuation_token

        async def pages() -> AsyncIterator[AsyncIterator[FakeEntity]]:
            for start in range(0, len(self._entities), self._page_size):
                values = self._entities[start : start + self._page_size]

                async def page(items: list[FakeEntity] = values) -> AsyncIterator[FakeEntity]:
                    for item in items:
                        yield item

                yield page()

        return pages()


class FakeTableClient:
    def __init__(self) -> None:
        self.entities: dict[tuple[str, str], FakeEntity] = {}
        self.transactions: list[list[tuple[Any, ...]]] = []
        self.close_calls = 0
        self.fail_transaction: Exception | None = None
        self.version = 0

    def _etag(self) -> str:
        self.version += 1
        return f'W/"{self.version}"'

    async def get_entity(self, *, partition_key: str, row_key: str) -> FakeEntity:
        try:
            return deepcopy(self.entities[(partition_key, row_key)])
        except KeyError as exc:
            raise ResourceNotFoundError("missing") from exc

    async def create_entity(self, entity: Mapping[str, object]) -> Mapping[str, object]:
        key = str(entity["PartitionKey"]), str(entity["RowKey"])
        if key in self.entities:
            raise ResourceExistsError("duplicate")
        stored = FakeEntity(deepcopy(entity), self._etag())
        self.entities[key] = stored
        return stored.metadata

    async def update_entity(
        self, entity: Mapping[str, object], *, mode: object, etag: str, match_condition: object
    ) -> Mapping[str, object]:
        del mode, match_condition
        key = str(entity["PartitionKey"]), str(entity["RowKey"])
        current = self.entities.get(key)
        if current is None:
            raise ResourceNotFoundError("missing")
        if current.metadata["etag"] != etag:
            raise ResourceModifiedError("stale")
        stored = FakeEntity(deepcopy(entity), self._etag())
        self.entities[key] = stored
        return stored.metadata

    async def delete_entity(
        self, *, partition_key: str, row_key: str, etag: str, match_condition: object
    ) -> None:
        del match_condition
        current = self.entities.get((partition_key, row_key))
        if current is None:
            raise ResourceNotFoundError("missing")
        if current.metadata["etag"] != etag:
            raise ResourceModifiedError("stale")
        del self.entities[(partition_key, row_key)]

    def query_entities(
        self, query_filter: str, *, parameters: Mapping[str, object] | None = None
    ) -> FakePages:
        values = list(self.entities.values())
        parameters = parameters or {}
        if "partition" in parameters:
            values = [v for v in values if v["PartitionKey"] == parameters["partition"]]
        if "row" in parameters:
            values = [v for v in values if v["RowKey"] == parameters["row"]]
        if "pending" in query_filter:
            values = [v for v in values if v.get("Sent") is False]
        return FakePages([deepcopy(value) for value in values])

    async def submit_transaction(self, operations: list[tuple[Any, ...]]) -> list[Mapping[str, object]]:
        self.transactions.append(deepcopy(operations))
        if self.fail_transaction is not None:
            raise self.fail_transaction
        snapshot = deepcopy(self.entities)
        responses: list[Mapping[str, object]] = []
        for operation in operations:
            action, entity = operation[0], operation[1]
            key = str(entity["PartitionKey"]), str(entity["RowKey"])
            if action == "create":
                if key in snapshot:
                    raise ResourceExistsError("duplicate")
                stored = FakeEntity(deepcopy(entity), self._etag())
            else:
                options = operation[2]
                current = snapshot.get(key)
                if current is None:
                    raise ResourceNotFoundError("missing")
                if current.metadata["etag"] != options["etag"]:
                    raise ResourceModifiedError("stale")
                stored = FakeEntity(deepcopy(entity), self._etag())
            snapshot[key] = stored
            responses.append(stored.metadata)
        self.entities = snapshot
        return responses

    async def close(self) -> None:
        self.close_calls += 1


def session(key: str = SESSION_KEY, *, count: int = 1) -> SessionRecord:
    return SessionRecord(
        session_key=key,
        created_at=NOW,
        expires_at=NOW,
        document_count=count,
        total_bytes=123,
        question_timestamps=(NOW,),
    )


def document(key: str = SESSION_KEY, *, state: DocumentState = DocumentState.READY) -> DocumentRecord:
    return DocumentRecord(
        session_key=key,
        document_id=DOCUMENT_ID,
        file_name="résumé.pdf",
        content_type="application/pdf",
        size_bytes=123,
        blob_name=f"uploads/{key}/{DOCUMENT_ID}",
        state=state,
        created_at=NOW,
        updated_at=NOW,
        expires_at=NOW,
        document_type="invoice",
        title="Résumé",
        content_result_id="result-1",
        content_operation_url="https://example.test/operations/1",
        extraction={"invoice": {"total": 12.5, "paid": True}, "pages": [1, 2]},
        markdown_blob_name="derived/result.md",
        page_count=2,
        chunk_count=3,
        token_count=42,
        failure_code="retry_later",
        failure_retryable=True,
        deleted_at=NOW,
    )


def outbox(key: str = SESSION_KEY, *, outbox_id: str = "ingest:doc:1") -> OutboxRecord:
    return OutboxRecord(
        outbox_id=outbox_id,
        session_key=key,
        kind="ingestion",
        payload=IngestionMessage(
            version=1,
            session_key=key,
            document_id=DOCUMENT_ID,
            blob_name=f"uploads/{key}/{DOCUMENT_ID}",
            correlation_id=UUID("868fba2c-1695-42d4-af7f-79069e434b34"),
            enqueued_at=NOW,
            resume_stage="chunking",
        ),
        created_at=NOW,
    )


@pytest.fixture
def repository() -> tuple[TableApplicationRepository, FakeTableClient]:
    client = FakeTableClient()
    return TableApplicationRepository(client), client


async def test_complete_session_document_and_outbox_roundtrip_preserves_etags(
    repository: tuple[TableApplicationRepository, FakeTableClient],
) -> None:
    repo, _ = repository
    created_session, session_etag = await repo.create(session())
    created_document = await repo.documents.create(document())
    await repo.documents.put_outbox_for_test(outbox())

    assert created_session == session()
    assert (await repo.get(SESSION_KEY)) == (session(), session_etag)
    assert (await repo.documents.get(SESSION_KEY, DOCUMENT_ID)) == created_document
    assert (await repo.documents.get_pending_outbox("ingest:doc:1"))[0] == outbox()  # type: ignore[index]


async def test_replace_delete_duplicates_and_stale_etags_translate_to_conflict(
    repository: tuple[TableApplicationRepository, FakeTableClient],
) -> None:
    repo, _ = repository
    _, session_etag = await repo.create(session())
    versioned = await repo.documents.create(document())

    with pytest.raises(ConcurrencyConflict):
        await repo.create(session())
    with pytest.raises(ConcurrencyConflict):
        await repo.replace(session(count=2), 'W/"stale"')
    with pytest.raises(ConcurrencyConflict):
        await repo.documents.replace(document(), 'W/"stale"')
    with pytest.raises(ConcurrencyConflict):
        await repo.documents.delete(SESSION_KEY, DOCUMENT_ID, 'W/"stale"')

    assert (await repo.replace(session(count=2), session_etag))[0].document_count == 2
    await repo.documents.delete(SESSION_KEY, DOCUMENT_ID, versioned.etag)
    assert await repo.documents.get(SESSION_KEY, DOCUMENT_ID) is None


async def test_atomic_transactions_use_exact_forms_and_never_partially_commit(
    repository: tuple[TableApplicationRepository, FakeTableClient],
) -> None:
    repo, client = repository
    _, session_etag = await repo.create(session(count=0))
    updated = session(count=1)
    _, versioned = await repo.reserve_and_create(updated, session_etag, document())
    queued = document(state=DocumentState.QUEUED)
    await repo.documents.commit_queued_with_outbox(queued, versioned.etag, outbox())

    reserve_ops, queue_ops = client.transactions
    assert [(op[0], op[1]["RowKey"]) for op in reserve_ops] == [
        ("update", "session"), ("create", f"document:{DOCUMENT_ID}")
    ]
    assert reserve_ops[0][2]["etag"] == session_etag
    assert [(op[0], op[1]["RowKey"]) for op in queue_ops] == [
        ("update", f"document:{DOCUMENT_ID}"), ("create", "outbox:ingest:doc:1")
    ]

    failing = FakeTableClient()
    failing_repo = TableApplicationRepository(failing)
    _, etag = await failing_repo.create(session(count=0))
    failing.fail_transaction = ResourceExistsError("duplicate")
    with pytest.raises(ConcurrencyConflict):
        await failing_repo.reserve_and_create(updated, etag, document())
    assert await failing_repo.documents.get(SESSION_KEY, DOCUMENT_ID) is None
    assert (await failing_repo.get(SESSION_KEY))[0].document_count == 0  # type: ignore[index]


async def test_partition_listing_pagination_pending_limit_target_and_mark_sent(
    repository: tuple[TableApplicationRepository, FakeTableClient],
) -> None:
    repo, _ = repository
    await repo.documents.create(document())
    await repo.documents.create(document(OTHER_SESSION_KEY))
    await repo.documents.put_outbox_for_test(outbox(outbox_id="2"))
    await repo.documents.put_outbox_for_test(outbox(outbox_id="1"))

    listed = await repo.documents.list_for_session(SESSION_KEY)
    assert [item.value.session_key for item in listed] == [SESSION_KEY]
    pending = await repo.documents.list_pending_outbox(1)
    assert [item[0].outbox_id for item in pending] == ["1"]
    targeted = await repo.documents.get_pending_outbox("2")
    assert targeted is not None
    await repo.documents.mark_outbox_sent("2", targeted[1], NOW)
    assert await repo.documents.get_pending_outbox("2") is None


@pytest.mark.parametrize(
    ("property_name", "value"),
    [
        ("CodecVersion", 99),
        ("Payload", "not-json"),
        ("Payload", '{"sessionKey":"bad"}'),
        ("Payload", '{"sessionKey":"' + SESSION_KEY + '","createdAt":"not-utc"}'),
        ("Payload", '{"documentId":"not-a-uuid"}'),
    ],
)
async def test_malformed_stored_entities_raise_one_safe_stable_error(
    repository: tuple[TableApplicationRepository, FakeTableClient],
    property_name: str,
    value: object,
) -> None:
    repo, client = repository
    await repo.create(session())
    client.entities[(f"session:{SESSION_KEY}", "session")][property_name] = value

    with pytest.raises(RepositoryDataError, match=r"Stored table entity is invalid\.$"):
        await repo.get(SESSION_KEY)


async def test_storage_contains_only_primitives_and_no_sensitive_material(
    repository: tuple[TableApplicationRepository, FakeTableClient],
) -> None:
    repo, client = repository
    await repo.documents.create(document())
    entity = client.entities[(f"session:{SESSION_KEY}", f"document:{DOCUMENT_ID}")]

    assert all(isinstance(value, (str, int, float, bool)) for value in entity.values())
    persisted = str(entity).lower()
    assert "cookie" not in persisted
    assert "sig=" not in persisted
    assert "rawcontent" not in persisted


async def test_client_ownership_controls_idempotent_close() -> None:
    injected = FakeTableClient()
    repository = TableApplicationRepository(injected)
    await repository.aclose()
    await repository.aclose()
    assert injected.close_calls == 0

    owned = FakeTableClient()
    owned_repository = TableApplicationRepository(owned, owns_client=True)
    await owned_repository.aclose()
    await owned_repository.aclose()
    assert owned.close_calls == 1