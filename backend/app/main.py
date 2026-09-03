from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import Settings
from app.core.errors import AppError, app_error_handler, correlation_middleware
from app.core.readiness import ReadinessRegistry


def create_app(
    *,
    settings: Settings | None = None,
    readiness_registry: ReadinessRegistry | None = None,
) -> FastAPI:
    app = FastAPI(title="Content Understanding RAG Demo", version="0.1.0")
    app.state.settings = settings or Settings()

    if readiness_registry is None:
        readiness_registry = ReadinessRegistry()

        async def configuration_ready() -> bool:
            return True

        readiness_registry.register("configuration", configuration_ready)

    app.state.readiness_registry = readiness_registry
    app.middleware("http")(correlation_middleware)
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(health_router)
    return app


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:create_app", factory=True, host="0.0.0.0", port=8000)
