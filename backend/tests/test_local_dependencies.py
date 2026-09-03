from typing import Any

import pytest

from app.core.config import Settings
from app.main import create_local_dependencies


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
    ]
    assert dependencies.blob_store._uploads_container == "incoming"
    assert dependencies.blob_store._derived_container == "outputs"
    assert dependencies.blob_store._control_container == "locks"


def test_azurite_bundle_requires_explicit_opt_in_and_is_rejected_in_production() -> None:
    with pytest.raises(ValueError, match="AZURITE_TABLE_CONNECTION_STRING"):
        create_local_dependencies(Settings(app_mode="local"))
    with pytest.raises(ValueError, match="production"):
        Settings(
            app_mode="production", frontend_origin="https://frontend.example.com",
            azurite_table_connection_string="UseDevelopmentStorage=true",
        )