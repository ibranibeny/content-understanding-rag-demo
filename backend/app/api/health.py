from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.readiness import ReadinessRegistry

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    registry: ReadinessRegistry = request.app.state.readiness_registry
    failed = await registry.check()
    if failed:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "failed": failed},
        )
    return JSONResponse(status_code=200, content={"status": "ready"})
