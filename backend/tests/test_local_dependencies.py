from typing import Any

import pytest
from azure.core.exceptions import ResourceExistsError

from app.core.config import Settings
from app.main import create_app, create_local_dependencies
from app.repositories.memory_repository import MemoryChunkSearch
from app.services.blob_service import LocalBlobSasSigner


def test_azurite_bundle_uses_one_connection_string_for_table_blob_and_both_queues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=secret"
    calls: list[tuple[str, tuple[Any, ...]]] = []

    def table(connection_string: str, table_name: str):  # type: ignore[no-untyped-def]
        calls.append(("table", (connection_string, table_name)))
        return object()

    def blob(connection_string: str):  # type: ignore[no-untyped-def]
        calls.append(("blob", (connection_string,)))
        return object()

    def queue(connection_string: str, queue_name: str):  # type: ignore[no-untyped-def]
        calls.append(("queue", (connection_string, queue_name)))
        return object()

    monkeypatch.setattr("app.main.TableClient.from_connection_string", table)
    monkeypatch.setattr("app.main.BlobServiceClient.from_connection_string", blob)
    monkeypatch.setattr("app.main.QueueClient.from_connection_string", queue)
    settings = Settings(
        app_mode="test", azurite_table_connection_string=connection,
        table_name="records", uploads_container="incoming", derived_container="outputs",
        control_container="locks", ingestion_queue="ingest",
        content_result_cleanup_queue="cleanup",
    )

    dependencies = create_local_dependencies(settings)

    assert calls == [
        ("table", (connection, "records")),
        ("blob", (connection,)),
        ("queue", (connection, "ingest")),
        ("queue", (connection, "cleanup")),
        ("queue", (connection, "ingestion-poison")),
    ]
    assert dependencies.blob_store._uploads_container == "incoming"
    assert dependencies.blob_store._derived_container == "outputs"
    assert dependencies.blob_store._control_container == "locks"
    assert isinstance(dependencies.chunk_search, MemoryChunkSearch)
    assert isinstance(dependencies.blob_store._sas_signer, LocalBlobSasSigner)


def test_azurite_bundle_requires_explicit_opt_in_and_is_rejected_in_production() -> None:
    with pytest.raises(ValueError, match="AZURITE_TABLE_CONNECTION_STRING"):
        create_local_dependencies(Settings(app_mode="local"))
    with pytest.raises(ValueError, match="production"):
        Settings(
            app_mode="production", frontend_origin="https://frontend.example.com",
            azurite_table_connection_string="UseDevelopmentStorage=true",
        )


async def test_local_lifespan_idempotently_creates_exact_data_plane_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    closed: list[str] = []

    class Table:
        async def create_table(self) -> None:
            calls.append(("table", "records"))
            raise ResourceExistsError("exists")

        async def close(self) -> None:
            closed.append("table")

    class Container:
        def __init__(self, name: str) -> None:
            self.name = name

        async def create_container(self) -> None:
            calls.append(("container", self.name))
            if self.name == "outputs":
                raise ResourceExistsError("exists")

    class Blob:
        def get_container_client(self, name: str) -> Container:
            return Container(name)

        async def close(self) -> None:
            closed.append("blob")

    class Queue:
        def __init__(self, name: str) -> None:
            self.name = name

        async def create_queue(self) -> None:
            calls.append(("queue", self.name))
            if self.name == "cleanup":
                raise ResourceExistsError("exists")

        async def close(self) -> None:
            closed.append(self.name)

    table = Table()
    blob = Blob()
    queues: dict[str, Queue] = {}
    monkeypatch.setattr(
        "app.main.TableClient.from_connection_string", lambda *args: table
    )
    monkeypatch.setattr(
        "app.main.BlobServiceClient.from_connection_string", lambda *args: blob
    )

    def queue_factory(connection: str, name: str) -> Queue:
        del connection
        queue = Queue(name)
        queues[name] = queue
        return queue

    monkeypatch.setattr("app.main.QueueClient.from_connection_string", queue_factory)
    settings = Settings(
        app_mode="test",
        azurite_table_connection_string=(
            "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=secret"
        ),
        table_name="records",
        uploads_container="incoming",
        derived_container="outputs",
        control_container="locks",
        ingestion_queue="ingest",
        content_result_cleanup_queue="cleanup",
        ingestion_poison_queue="poison",
    )
    dependencies = create_local_dependencies(settings)

    from httpx import ASGITransport, AsyncClient

    app = create_app(settings=settings, dependencies=dependencies)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test"),
    ):
        pass

    assert calls == [
        ("table", "records"),
        ("container", "incoming"),
        ("container", "outputs"),
        ("container", "locks"),
        ("queue", "ingest"),
        ("queue", "cleanup"),
        ("queue", "poison"),
    ]
    assert set(closed) == {"table", "blob", "ingest", "cleanup", "poison"}


async def test_local_provisioning_failure_closes_every_client_and_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class Table:
        async def create_table(self) -> None:
            raise RuntimeError("provisioning failed")

        async def close(self) -> None:
            closed.append("table")

    class Blob:
        async def close(self) -> None:
            closed.append("blob")

    class Queue:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self) -> None:
            closed.append(self.name)

    monkeypatch.setattr("app.main.TableClient.from_connection_string", lambda *args: Table())
    monkeypatch.setattr("app.main.BlobServiceClient.from_connection_string", lambda *args: Blob())
    monkeypatch.setattr(
        "app.main.QueueClient.from_connection_string",
        lambda connection, name: Queue(name),
    )
    settings = Settings(
        app_mode="test",
        azurite_table_connection_string=(
            "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=secret"
        ),
    )
    dependencies = create_local_dependencies(settings)
    app = create_app(settings=settings, dependencies=dependencies)

    with pytest.raises(RuntimeError, match="provisioning failed"):
        async with app.router.lifespan_context(app):
            pass

    assert set(closed) == {
        "table",
        "blob",
        settings.ingestion_queue,
        settings.content_result_cleanup_queue,
        settings.ingestion_poison_queue,
    }