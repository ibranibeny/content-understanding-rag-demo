import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from azure.core.credentials_async import AsyncTokenCredential
from azure.data.tables.aio import TableClient
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient
from azure.storage.queue.aio import QueueClient
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.api.session import router as session_router
from app.api.uploads import router as uploads_router
from app.core.config import Settings
from app.core.errors import (
    AppError,
    app_error_handler,
    correlation_middleware,
    request_validation_error_handler,
)
from app.core.readiness import ReadinessRegistry
from app.domain.models import DocumentChunk, RetrievedEvidence
from app.domain.protocols import (
    BlobStore,
    ChunkSearch,
    DocumentRepository,
    IngestionBacklog,
    ReadinessCheck,
    SessionDocumentRepository,
    SessionRepository,
    UploadBlobStore,
    WorkQueue,
)
from app.repositories.memory_repository import (
    MemoryApplicationRepository,
    MemorySessionRepository,
    MemoryWorkQueue,
)
from app.repositories.table_repository import TableApplicationRepository, TableClientLike
from app.services.blob_service import AzureBlobStore, BlobServiceClientLike
from app.services.deletion_service import DeletionService
from app.services.document_service import DocumentService
from app.services.outbox_service import OutboxDispatcher
from app.services.queue_service import AzureWorkQueue, QueueClientLike
from app.services.session_service import SessionService, SystemClock
from app.services.upload_service import UploadService

PRODUCTION_READINESS_CHECKS = frozenset({"blob", "queue", "table", "search", "foundry"})


async def _ready() -> bool:
    return True


async def _not_ready() -> bool:
    return False


class ApplicationDispatcher(Protocol):
    async def dispatch_once(self) -> int: ...

    async def dispatch_outbox(self, outbox_id: str) -> bool: ...

    async def run(self) -> None: ...


class ApplicationRepository(SessionDocumentRepository, Protocol):
    sessions: SessionRepository
    documents: DocumentRepository


class ApplicationWorkQueue(WorkQueue, IngestionBacklog, Protocol):
    pass


class CloseableApplicationRepository(ApplicationRepository, Protocol):
    async def aclose(self) -> None: ...


class CloseableApplicationWorkQueue(ApplicationWorkQueue, Protocol):
    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ApplicationDependencies:
    application_repository: ApplicationRepository
    work_queue: ApplicationWorkQueue
    blob_store: BlobStore
    chunk_search: ChunkSearch
    readiness_checks: Mapping[str, ReadinessCheck]


@dataclass(frozen=True, slots=True)
class ProductionDependencies(ApplicationDependencies):
    application_repository: CloseableApplicationRepository
    work_queue: CloseableApplicationWorkQueue
    credential: AsyncTokenCredential

    async def aclose(self) -> None:
        results = await asyncio.gather(
            self.application_repository.aclose(),
            self.work_queue.aclose(),
            self.blob_store.aclose(),
            self.credential.close(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result


@dataclass(frozen=True, slots=True)
class LocalDependencies(ApplicationDependencies):
    application_repository: CloseableApplicationRepository
    work_queue: CloseableApplicationWorkQueue

    async def aclose(self) -> None:
        results = await asyncio.gather(
            self.application_repository.aclose(),
            self.work_queue.aclose(),
            self.blob_store.aclose(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result


def create_production_dependencies(
    settings: Settings,
    chunk_search: ChunkSearch | None = None,
    readiness_checks: Mapping[str, ReadinessCheck] | None = None,
    *,
    credential: AsyncTokenCredential | None = None,
    repository_factory: Callable[
        [Settings, AsyncTokenCredential], CloseableApplicationRepository
    ] | None = None,
    queue_factory: Callable[[Settings, AsyncTokenCredential], CloseableApplicationWorkQueue]
    | None = None,
    blob_factory: Callable[[Settings, AsyncTokenCredential], BlobStore] | None = None,
) -> ApplicationDependencies:
    if settings.app_mode != "production":
        raise ValueError("production dependencies require production settings")
    try:
        actual_credential = credential or DefaultAzureCredential()
    except ImportError as exc:
        raise ValueError(
            "production Azure clients require the aiohttp async transport"
        ) from exc
    repository = (
        repository_factory(settings, actual_credential)
        if repository_factory is not None
        else TableApplicationRepository.from_settings(
            settings, credential=actual_credential
        )
    )
    queue = (
        queue_factory(settings, actual_credential)
        if queue_factory is not None
        else AzureWorkQueue.from_settings(settings, actual_credential)
    )
    blobs = (
        blob_factory(settings, actual_credential)
        if blob_factory is not None
        else AzureBlobStore(
            settings.storage_account_name,
            settings.uploads_container,
            SystemClock(),
            derived_container=settings.derived_container,
            control_container=settings.control_container,
            credential=actual_credential,
        )
    )
    return ProductionDependencies(
        application_repository=repository,
        work_queue=queue,
        blob_store=blobs,
        chunk_search=chunk_search or _EmptyChunkSearch(),
        readiness_checks=readiness_checks or {
            name: _not_ready for name in PRODUCTION_READINESS_CHECKS
        },
        credential=actual_credential,
    )


class _EmptyChunkSearch:
    async def delete_for_document(self, session_key: str, document_id: UUID) -> None:
        del session_key, document_id

    async def has_for_document(self, session_key: str, document_id: UUID) -> bool:
        del session_key, document_id
        return False

    async def upsert(self, chunks: Sequence[DocumentChunk]) -> None:
        del chunks

    async def search(
        self,
        session_key: str,
        query: str,
        vector: Sequence[float],
        document_ids: Sequence[UUID],
    ) -> list[RetrievedEvidence]:
        del session_key, query, vector, document_ids
        return []


def create_local_dependencies(
    settings: Settings,
    *,
    chunk_search: ChunkSearch | None = None,
) -> LocalDependencies:
    connection_string = settings.azurite_table_connection_string
    if settings.app_mode == "production":
        raise ValueError("production must not use Azurite dependencies")
    if not connection_string:
        raise ValueError("AZURITE_TABLE_CONNECTION_STRING is required")
    table_client = TableClient.from_connection_string(connection_string, settings.table_name)
    blob_client = BlobServiceClient.from_connection_string(connection_string)
    ingestion = QueueClient.from_connection_string(
        connection_string, settings.ingestion_queue
    )
    cleanup = QueueClient.from_connection_string(
        connection_string, settings.content_result_cleanup_queue
    )
    repository = TableApplicationRepository(
        cast(TableClientLike, table_client), owns_client=True
    )
    queue = AzureWorkQueue(
        cast(QueueClientLike, ingestion),
        cast(QueueClientLike, cleanup),
        owns_clients=True,
    )
    blobs = AzureBlobStore(
        settings.storage_account_name,
        settings.uploads_container,
        SystemClock(),
        derived_container=settings.derived_container,
        control_container=settings.control_container,
        service_client=cast(BlobServiceClientLike, blob_client),
        own_service_client=True,
    )
    return LocalDependencies(
        application_repository=repository,
        work_queue=queue,
        blob_store=blobs,
        chunk_search=chunk_search or _EmptyChunkSearch(),
        readiness_checks={"configuration": _ready},
    )


def create_app(
    settings: Settings | None = None,
    readiness_checks: Mapping[str, ReadinessCheck] | None = None,
    session_service: SessionService | None = None,
    upload_service: UploadService | None = None,
    outbox_dispatcher: ApplicationDispatcher | None = None,
    document_service: DocumentService | None = None,
    deletion_service: DeletionService | None = None,
    *,
    dependencies: ApplicationDependencies | None = None,
    enable_outbox_dispatcher: bool | None = None,
) -> FastAPI:
    actual_settings = settings or Settings()
    if dependencies is not None and any(
        value is not None
        for value in (
            readiness_checks,
            session_service,
            upload_service,
            outbox_dispatcher,
            document_service,
            deletion_service,
        )
    ):
        raise ValueError("dependencies cannot be combined with individual service injection")
    if actual_settings.app_mode == "production" and dependencies is None:
        raise ValueError("production requires explicit ApplicationDependencies")
    if (
        actual_settings.app_mode == "production"
        and dependencies is not None
        and (
            isinstance(dependencies.application_repository, MemoryApplicationRepository)
            or isinstance(dependencies.work_queue, MemoryWorkQueue)
        )
    ):
        raise ValueError("production dependencies must not use memory adapters")
    if (upload_service is None) != (outbox_dispatcher is None):
        raise ValueError(
            "upload_service and outbox_dispatcher must be injected together"
        )
    if (document_service is None) != (deletion_service is None):
        raise ValueError("document_service and deletion_service must be injected together")
    clock = SystemClock()
    application_repository = (
        dependencies.application_repository
        if dependencies is not None
        else MemoryApplicationRepository()
    )
    actual_session_service = session_service or SessionService(
        application_repository.sessions,
        clock,
        settings=actual_settings,
        session_documents=application_repository,
    )
    documents: DocumentRepository
    if upload_service is not None:
        documents = upload_service.documents
    elif session_service is None:
        documents = application_repository.documents
    elif isinstance(actual_session_service.repository, MemorySessionRepository):
        documents = actual_session_service.repository.document_repository()
    else:
        raise ValueError(
            "custom session_service requires upload_service and outbox_dispatcher"
        )
    queue: ApplicationWorkQueue = (
        dependencies.work_queue if dependencies is not None else MemoryWorkQueue()
    )
    actual_dispatcher = outbox_dispatcher or OutboxDispatcher(documents, queue, clock)
    owned_blob_store: AzureBlobStore | None = None
    if upload_service is None:
        if dependencies is not None:
            actual_blob_store: UploadBlobStore = dependencies.blob_store
        else:
            owned_blob_store = AzureBlobStore(
                actual_settings.storage_account_name,
                actual_settings.uploads_container,
                clock,
                derived_container=actual_settings.derived_container,
                control_container=actual_settings.control_container,
            )
            actual_blob_store = owned_blob_store
        actual_upload_service = UploadService(
            actual_session_service,
            documents,
            actual_blob_store,
            actual_dispatcher,
            clock,
            queue,
        )
    else:
        actual_upload_service = upload_service

    if document_service is None:
        actual_deletion_service = DeletionService(
            documents,
            actual_upload_service.blobs,  # type: ignore[arg-type]
            dependencies.chunk_search if dependencies is not None else _EmptyChunkSearch(),
        )
        actual_document_service = DocumentService(
            documents, actual_deletion_service, actual_dispatcher, clock
        )
    else:
        assert deletion_service is not None
        if document_service.deletion_service is not deletion_service:
            raise ValueError("document_service must use the injected deletion_service")
        if document_service.repository is not actual_upload_service.documents:
            raise ValueError("document and upload services must use one repository")
        actual_deletion_service = deletion_service
        actual_document_service = document_service
    dispatcher_enabled = (
        actual_settings.app_mode == "production"
        if enable_outbox_dispatcher is None
        else enable_outbox_dispatcher
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if dispatcher_enabled:
            task = asyncio.create_task(actual_dispatcher.run())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if owned_blob_store is not None:
                await actual_upload_service.aclose()
            if isinstance(dependencies, (ProductionDependencies, LocalDependencies)):
                await dependencies.aclose()

    app = FastAPI(
        title="Content Understanding RAG Demo", version="0.1.0", lifespan=lifespan
    )
    app.state.settings = actual_settings
    app.state.session_service = actual_session_service
    app.state.upload_service = actual_upload_service
    app.state.outbox_dispatcher = actual_dispatcher
    app.state.work_queue = queue
    app.state.document_repository = documents
    app.state.deletion_service = actual_deletion_service
    app.state.document_service = actual_document_service

    if dependencies is not None:
        readiness_checks = dependencies.readiness_checks
    if readiness_checks is None:
        if app.state.settings.app_mode == "production":
            readiness_checks = {name: _not_ready for name in PRODUCTION_READINESS_CHECKS}
        else:
            readiness_checks = {"configuration": _ready}
    elif app.state.settings.app_mode == "production":
        supplied_names = set(readiness_checks)
        if supplied_names != PRODUCTION_READINESS_CHECKS:
            required = ", ".join(sorted(PRODUCTION_READINESS_CHECKS))
            raise ValueError(f"production readiness checks must contain exactly: {required}")

    readiness_registry = ReadinessRegistry()
    for name, check in readiness_checks.items():
        readiness_registry.register(name, check)

    app.state.readiness_registry = readiness_registry
    app.middleware("http")(correlation_middleware)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.include_router(health_router)
    app.include_router(session_router)
    app.include_router(uploads_router)
    app.include_router(documents_router)
    return app


def create_production_app() -> FastAPI:
    settings = Settings()
    if settings.app_mode != "production":
        raise ValueError("production app factory requires APP_MODE=production")
    dependencies = create_production_dependencies(settings)
    try:
        return create_app(settings=settings, dependencies=dependencies)
    except BaseException:
        asyncio.run(cast(ProductionDependencies, dependencies).aclose())
        raise


def create_local_app() -> FastAPI:
    settings = Settings()
    dependencies = create_local_dependencies(settings)
    return create_app(settings=settings, dependencies=dependencies)


def run() -> None:
    import uvicorn

    mode = os.getenv("APP_MODE", "local")
    if mode == "production":
        factory = "app.main:create_production_app"
    elif os.getenv("AZURITE_TABLE_CONNECTION_STRING"):
        factory = "app.main:create_local_app"
    else:
        factory = "app.main:create_app"
    uvicorn.run(factory, factory=True, host="0.0.0.0", port=8000)
