from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from inspect import signature
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.models import DocumentChunk, RetrievedEvidence
from app.main import (
    ApplicationDependencies,
    create_app,
    create_production_app,
    create_production_dependencies,
    run,
)
from app.repositories.memory_repository import MemoryApplicationRepository, MemoryWorkQueue
from app.repositories.table_repository import TableApplicationRepository
from app.services.blob_service import AzureBlobStore, UserDelegationBlobSasSigner
from app.services.queue_service import AzureWorkQueue
from app.services.search_service import AzureSearchService


async def ready() -> bool:
    return True


class Blobs:
    closed = 0

    def __init__(self) -> None:
        self.readiness_calls = 0

    async def is_ready(self) -> bool:
        self.readiness_calls += 1
        return True

    async def create_upload(self, blob_name: str, content_type: str) -> Any:
        raise AssertionError("not used")

    async def verify_upload(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("not used")

    @asynccontextmanager
    async def acquire_document_lease(
        self, session_key: str, document_id: UUID
    ) -> AsyncIterator[Any]:
        del session_key, document_id
        raise AssertionError("not used")
        yield

    async def delete_document_artifacts(self, session_key: str, document_id: UUID) -> None:
        del session_key, document_id

    async def document_artifacts_exist(self, session_key: str, document_id: UUID) -> bool:
        del session_key, document_id
        return False

    async def aclose(self) -> None:
        self.closed += 1


class Search:
    def __init__(self) -> None:
        self.readiness_calls = 0

    async def is_ready(self) -> bool:
        self.readiness_calls += 1
        return True

    async def aclose(self) -> None:
        return None

    async def delete_for_document(self, session_key: str, document_id: UUID) -> None:
        del session_key, document_id

    async def has_for_document(self, session_key: str, document_id: UUID) -> bool:
        del session_key, document_id
        return False

    async def upsert(self, chunks: list[DocumentChunk]) -> None:
        del chunks

    async def search(
        self,
        session_key: str,
        query: str,
        vector: list[float],
        document_ids: list[UUID],
    ) -> list[RetrievedEvidence]:
        del session_key, query, vector, document_ids
        return []


class DurableRepository:
    def __init__(self) -> None:
        self.backing = MemoryApplicationRepository()
        self.sessions = self.backing.sessions
        self.documents = self.backing.documents
        self.readiness_calls = 0

    async def is_ready(self) -> bool:
        self.readiness_calls += 1
        return True

    async def aclose(self) -> None:
        return None


def production_settings() -> Settings:
    return Settings(app_mode="production", frontend_origin="https://frontend.example.com")


def test_production_rejects_omitted_dependency_bundle_instead_of_using_memory() -> None:
    with pytest.raises(ValueError, match="production requires explicit ApplicationDependencies"):
        create_app(settings=production_settings())


def test_production_dependency_factory_does_not_accept_keys_or_require_search_injection() -> None:
    parameters = signature(create_production_dependencies).parameters
    assert "chunk_search" not in parameters
    assert "account_key" not in parameters
    assert "sas_signer" not in parameters


def test_production_wires_one_explicit_dependency_bundle_without_memory_defaults() -> None:
    repository = DurableRepository()
    queue = AzureWorkQueue(QueueClient(), QueueClient(), owns_clients=True)
    blobs = Blobs()
    search = Search()
    dependencies = ApplicationDependencies(
        application_repository=repository,
        work_queue=queue,
        blob_store=blobs,
        chunk_search=search,
        readiness_checks={
            name: ready for name in ("blob", "queue", "table", "search", "foundry")
        },
    )

    app = create_app(settings=production_settings(), dependencies=dependencies)

    assert app.state.document_repository is repository.documents
    assert app.state.work_queue is queue
    assert app.state.upload_service.blobs is blobs
    assert app.state.deletion_service._search is search
    assert app.state.session_service.repository is repository.sessions
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
    assert blobs.closed == 0


def test_production_bundle_rejects_memory_repository_or_queue() -> None:
    repository = MemoryApplicationRepository()
    checks = {name: ready for name in ("blob", "queue", "table", "search", "foundry")}
    dependencies = ApplicationDependencies(
        application_repository=repository,
        work_queue=MemoryWorkQueue(),
        blob_store=Blobs(),
        chunk_search=Search(),
        readiness_checks=checks,
    )

    with pytest.raises(ValueError, match="production dependencies must not use memory"):
        create_app(settings=production_settings(), dependencies=dependencies)


class QueueClient:
    def __init__(self) -> None:
        self.properties_calls = 0

    async def send_message(self, content: str) -> None:
        del content

    async def get_queue_properties(self):  # type: ignore[no-untyped-def]
        self.properties_calls += 1
        return type("Properties", (), {"approximate_message_count": 0})()

    async def close(self) -> None:
        return None


class Credential:
    def __init__(self) -> None:
        self.close_calls = 0

    async def get_token(self, *scopes: str, **kwargs: object):  # type: ignore[no-untyped-def]
        del scopes, kwargs
        raise AssertionError("no network access expected")

    async def close(self) -> None:
        self.close_calls += 1


class TokenCredential(Credential):
    def __init__(self) -> None:
        super().__init__()
        self.scopes: list[str] = []

    async def get_token(self, *scopes: str, **kwargs: object):  # type: ignore[no-untyped-def]
        del kwargs
        self.scopes.extend(scopes)
        return type("Token", (), {"token": "managed-identity-token"})()


async def test_default_production_readiness_probes_dependencies_without_model_calls() -> None:
    credential = TokenCredential()
    repository = DurableRepository()
    ingestion = QueueClient()
    cleanup = QueueClient()
    queue = AzureWorkQueue(ingestion, cleanup, owns_clients=True)
    blobs = Blobs()
    search = Search()
    dependencies = create_production_dependencies(
        production_settings(),
        credential=credential,  # type: ignore[arg-type]
        repository_factory=lambda settings, credential: repository,  # type: ignore[arg-type]
        queue_factory=lambda settings, credential: queue,
        blob_factory=lambda settings, credential: blobs,
        search_factory=lambda settings, credential: search,
    )

    results = {
        name: await check() for name, check in dependencies.readiness_checks.items()
    }

    assert results == {
        "blob": True,
        "queue": True,
        "table": True,
        "search": True,
        "foundry": True,
    }
    assert blobs.readiness_calls == 1
    assert ingestion.properties_calls == cleanup.properties_calls == 1
    assert repository.readiness_calls == search.readiness_calls == 1
    assert set(credential.scopes) == {
        "https://ai.azure.com/.default",
        "https://cognitiveservices.azure.com/.default",
    }

    await dependencies.aclose()  # type: ignore[attr-defined]


def test_production_helper_constructs_table_queue_and_three_container_blob_graph() -> None:
    credential = Credential()
    settings = Settings(
        app_mode="production",
        frontend_origin="https://frontend.example.com",
        storage_account_name="durableacct",
        uploads_container="incoming",
        derived_container="outputs",
        control_container="locks",
    )
    class Closable:
        async def close(self) -> None:
            return None

    table_client = Closable()
    repository = TableApplicationRepository(table_client, owns_client=True)  # type: ignore[arg-type]
    queue = AzureWorkQueue(QueueClient(), QueueClient(), owns_clients=True)
    blobs = AzureBlobStore(
        "durableacct",
        "incoming",
        type("Clock", (), {"now": lambda self: None})(),  # type: ignore[arg-type]
        derived_container="outputs",
        control_container="locks",
        service_client=Closable(),  # type: ignore[arg-type]
        own_service_client=True,
    )
    dependencies = create_production_dependencies(
        settings,
        {name: ready for name in ("blob", "queue", "table", "search", "foundry")},
        credential=credential,  # type: ignore[arg-type]
        repository_factory=lambda settings, credential: repository,
        queue_factory=lambda settings, credential: queue,
        blob_factory=lambda settings, credential: blobs,
        search_factory=lambda settings, credential: Search(),
    )

    assert isinstance(dependencies.application_repository, TableApplicationRepository)
    assert isinstance(dependencies.work_queue, AzureWorkQueue)
    assert isinstance(dependencies.blob_store, AzureBlobStore)
    assert dependencies.blob_store._uploads_container == "incoming"
    assert dependencies.blob_store._derived_container == "outputs"
    assert dependencies.blob_store._control_container == "locks"
    assert isinstance(
        dependencies.blob_store._sas_signer, UserDelegationBlobSasSigner
    )

    with TestClient(create_app(settings=settings, dependencies=dependencies)):
        pass
    assert credential.close_calls == 1


def test_production_factory_loads_settings_builds_dependencies_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = Credential()
    repository = DurableRepository()
    queue = AzureWorkQueue(QueueClient(), QueueClient())
    blobs = Blobs()
    dependencies = create_production_dependencies(
        production_settings(),
        {name: ready for name in ("blob", "queue", "table", "search", "foundry")},
        credential=credential,  # type: ignore[arg-type]
        repository_factory=lambda settings, credential: repository,  # type: ignore[arg-type]
        queue_factory=lambda settings, credential: queue,
        blob_factory=lambda settings, credential: blobs,
        search_factory=lambda settings, credential: Search(),
    )
    monkeypatch.setenv("APP_MODE", "production")
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://frontend.example.com")
    monkeypatch.setattr(
        "app.main.create_production_dependencies", lambda settings: dependencies
    )

    app = create_production_app()
    assert app.state.document_repository is repository.documents
    with TestClient(app):
        pass
    assert credential.close_calls == 1
    assert blobs.closed == 1


def test_production_factory_closes_dependencies_once_when_app_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = Credential()
    repository = DurableRepository()
    queue = AzureWorkQueue(QueueClient(), QueueClient(), owns_clients=True)
    blobs = Blobs()
    dependencies = create_production_dependencies(
        production_settings(),
        {name: ready for name in ("blob", "queue", "table", "search", "foundry")},
        credential=credential,  # type: ignore[arg-type]
        repository_factory=lambda settings, credential: repository,  # type: ignore[arg-type]
        queue_factory=lambda settings, credential: queue,
        blob_factory=lambda settings, credential: blobs,
        search_factory=lambda settings, credential: Search(),
    )
    monkeypatch.setenv("APP_MODE", "production")
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://frontend.example.com")
    monkeypatch.setattr(
        "app.main.create_production_dependencies", lambda settings: dependencies
    )
    monkeypatch.setattr(
        "app.main.create_app",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("construction failed")),
    )

    with pytest.raises(RuntimeError, match="construction failed"):
        create_production_app()
    assert credential.close_calls == 1
    assert blobs.closed == 1


async def test_production_close_attempts_every_resource_when_one_close_fails() -> None:
    closed: list[str] = []

    class FailingRepository(DurableRepository):
        async def aclose(self) -> None:
            closed.append("repository")
            raise RuntimeError("repository close failed")

    class ClosingQueue(AzureWorkQueue):
        async def aclose(self) -> None:
            closed.append("queue")

    class ClosingBlobs(Blobs):
        async def aclose(self) -> None:
            closed.append("blobs")

    class ClosingCredential(Credential):
        async def close(self) -> None:
            closed.append("credential")

    dependencies = create_production_dependencies(
        production_settings(),
        {name: ready for name in ("blob", "queue", "table", "search", "foundry")},
        credential=ClosingCredential(),  # type: ignore[arg-type]
        repository_factory=lambda settings, credential: FailingRepository(),  # type: ignore[arg-type]
        queue_factory=lambda settings, credential: ClosingQueue(QueueClient(), QueueClient()),
        blob_factory=lambda settings, credential: ClosingBlobs(),
        search_factory=lambda settings, credential: Search(),
    )

    with pytest.raises(RuntimeError, match="repository close failed"):
        await dependencies.aclose()  # type: ignore[attr-defined]
    assert set(closed) == {"repository", "queue", "blobs", "credential"}


async def test_production_constructs_closes_and_uses_real_search_as_chunk_search() -> None:
    credential = Credential()
    repository = DurableRepository()
    queue = AzureWorkQueue(QueueClient(), QueueClient())
    blobs = Blobs()
    search = Search()
    search.closed = 0  # type: ignore[attr-defined]

    async def close_search() -> None:
        search.closed += 1  # type: ignore[attr-defined]

    search.aclose = close_search  # type: ignore[attr-defined,method-assign]
    dependencies = create_production_dependencies(
        production_settings(),
        credential=credential,  # type: ignore[arg-type]
        repository_factory=lambda settings, credential: repository,  # type: ignore[arg-type]
        queue_factory=lambda settings, credential: queue,
        blob_factory=lambda settings, credential: blobs,
        search_factory=lambda settings, credential: search,  # type: ignore[arg-type]
    )

    assert dependencies.chunk_search is search
    assert dependencies.readiness_checks["search"] == search.is_ready  # type: ignore[attr-defined]
    await dependencies.aclose()  # type: ignore[attr-defined]
    assert search.closed == 1  # type: ignore[attr-defined]


def test_default_production_search_factory_builds_keyless_sdk_adapter() -> None:
    credential = Credential()
    repository = DurableRepository()
    queue = AzureWorkQueue(QueueClient(), QueueClient())
    blobs = Blobs()
    dependencies = create_production_dependencies(
        production_settings(),
        credential=credential,  # type: ignore[arg-type]
        repository_factory=lambda settings, credential: repository,  # type: ignore[arg-type]
        queue_factory=lambda settings, credential: queue,
        blob_factory=lambda settings, credential: blobs,
    )

    assert isinstance(dependencies.chunk_search, AzureSearchService)


def test_cli_selects_production_or_explicit_azurite_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append({"args": args, **kwargs}))

    monkeypatch.setenv("APP_MODE", "production")
    run()
    assert calls[-1]["args"] == ("app.main:create_production_app",)

    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("AZURITE_TABLE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    run()
    assert calls[-1]["args"] == ("app.main:create_local_app",)