"""The worker must run against Azurite locally and Entra-authenticated Azure in production."""

from typing import Any, cast

import pytest
from azure.identity.aio import DefaultAzureCredential
from azure.storage.queue.aio import QueueClient

from app.core.config import Settings
from app.worker import SystemClock, build_worker_storage

CONNECTION = "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=secret"


def test_local_mode_wires_storage_from_azurite_connection_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    def record_queue(connection_string: str, queue_name: str) -> object:
        calls.append(("queue", (connection_string, queue_name)))
        return object()

    def record_table(connection_string: str, table_name: str) -> object:
        calls.append(("table", (connection_string, table_name)))
        return object()

    def record_blob(connection_string: str) -> object:
        calls.append(("blob", (connection_string,)))
        return object()

    def forbid_account_url(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("local mode must not open an Entra-authenticated account URL")

    monkeypatch.setattr("app.worker.QueueClient.from_connection_string", record_queue)
    monkeypatch.setattr("app.worker.TableClient.from_connection_string", record_table)
    monkeypatch.setattr("app.worker.BlobServiceClient.from_connection_string", record_blob)
    monkeypatch.setattr("app.worker.QueueClient.__init__", forbid_account_url)

    settings = Settings(
        app_mode="local",
        azurite_table_connection_string=CONNECTION,
        table_name="records",
        ingestion_queue="ingest",
        content_result_cleanup_queue="cleanup",
        ingestion_poison_queue="poison",
    )

    build_worker_storage(settings, SystemClock(), cast(DefaultAzureCredential, object()))

    assert ("queue", (CONNECTION, "ingest")) in calls
    assert ("queue", (CONNECTION, "cleanup")) in calls
    assert ("queue", (CONNECTION, "poison")) in calls
    assert ("table", (CONNECTION, "records")) in calls
    assert ("blob", (CONNECTION,)) in calls


def test_production_mode_uses_entra_account_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid_connection_string(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("production must not use an Azurite connection string")

    monkeypatch.setattr(
        "app.worker.QueueClient.from_connection_string", forbid_connection_string
    )
    settings = Settings(app_mode="production", frontend_origin="https://app.example.com")

    storage = build_worker_storage(settings, SystemClock(), DefaultAzureCredential())

    assert isinstance(storage.ingestion_client, QueueClient)
