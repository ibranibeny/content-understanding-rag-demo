import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from typing import Protocol

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

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
from app.domain.protocols import ReadinessCheck
from app.repositories.memory_repository import (
    MemoryApplicationRepository,
    MemoryDocumentRepository,
    MemorySessionRepository,
    MemoryWorkQueue,
)
from app.services.blob_service import AzureBlobStore
from app.services.outbox_service import OutboxDispatcher
from app.services.session_service import SessionService, SystemClock
from app.services.upload_service import UploadService

PRODUCTION_READINESS_CHECKS = frozenset({"blob", "queue", "table", "search", "foundry"})


async def _ready() -> bool:
    return True


async def _not_ready() -> bool:
    return False


class ApplicationDispatcher(Protocol):
    async def dispatch_once(self) -> int: ...

    async def run(self) -> None: ...


def create_app(
    settings: Settings | None = None,
    readiness_checks: Mapping[str, ReadinessCheck] | None = None,
    session_service: SessionService | None = None,
    upload_service: UploadService | None = None,
    outbox_dispatcher: ApplicationDispatcher | None = None,
    *,
    enable_outbox_dispatcher: bool | None = None,
) -> FastAPI:
    if (upload_service is None) != (outbox_dispatcher is None):
        raise ValueError(
            "upload_service and outbox_dispatcher must be injected together"
        )
    actual_settings = settings or Settings()
    clock = SystemClock()
    application_repository = MemoryApplicationRepository()
    actual_session_service = session_service or SessionService(
        application_repository.sessions,
        clock,
        settings=actual_settings,
        session_documents=application_repository,
    )
    if session_service is None:
        documents = application_repository.documents
    elif isinstance(actual_session_service.repository, MemorySessionRepository):
        documents = actual_session_service.repository.document_repository()
    elif upload_service is None:
        raise ValueError(
            "custom session_service requires upload_service and outbox_dispatcher"
        )
    else:
        documents = MemoryDocumentRepository()
    queue = MemoryWorkQueue()
    actual_dispatcher = outbox_dispatcher or OutboxDispatcher(documents, queue, clock)
    actual_upload_service = upload_service or UploadService(
        actual_session_service,
        documents,
        AzureBlobStore(
            actual_settings.storage_account_name,
            actual_settings.uploads_container,
            clock,
        ),
        actual_dispatcher,
        clock,
    )
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
            await actual_upload_service.aclose()

    app = FastAPI(
        title="Content Understanding RAG Demo", version="0.1.0", lifespan=lifespan
    )
    app.state.settings = actual_settings
    app.state.session_service = actual_session_service
    app.state.upload_service = actual_upload_service
    app.state.outbox_dispatcher = actual_dispatcher

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
    return app


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:create_app", factory=True, host="0.0.0.0", port=8000)
