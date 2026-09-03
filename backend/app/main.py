from collections.abc import Mapping

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import Settings
from app.core.errors import AppError, app_error_handler, correlation_middleware
from app.core.readiness import ReadinessRegistry
from app.domain.protocols import ReadinessCheck

PRODUCTION_READINESS_CHECKS = frozenset({"blob", "queue", "table", "search", "foundry"})


async def _ready() -> bool:
    return True


async def _not_ready() -> bool:
    return False


def create_app(
    settings: Settings | None = None,
    readiness_checks: Mapping[str, ReadinessCheck] | None = None,
) -> FastAPI:
    app = FastAPI(title="Content Understanding RAG Demo", version="0.1.0")
    app.state.settings = settings or Settings()

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
    app.include_router(health_router)
    return app


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:create_app", factory=True, host="0.0.0.0", port=8000)
