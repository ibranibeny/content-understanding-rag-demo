import asyncio
from time import perf_counter

from fastapi.testclient import TestClient

from app.core.readiness import ReadinessRegistry
from app.main import create_app


async def passing_check() -> bool:
    return True


async def failing_check() -> bool:
    return False


async def leaking_check() -> bool:
    raise RuntimeError("secret credential text")


async def slow_check() -> bool:
    await asyncio.sleep(1)
    return True


async def cancellation_resistant_check() -> bool:
    try:
        await asyncio.sleep(1)
    except asyncio.CancelledError:
        await asyncio.sleep(0.2)
    return True


async def test_registry_reports_sorted_named_failures() -> None:
    registry = ReadinessRegistry(timeout_seconds=0.1)
    registry.register("table", passing_check)
    registry.register("search", failing_check)
    registry.register("blob", leaking_check)

    assert await registry.check() == ["blob", "search"]


async def test_registry_reports_timed_out_check_by_name() -> None:
    registry = ReadinessRegistry(timeout_seconds=0.01)
    registry.register("search", slow_check)

    assert await registry.check() == ["search"]


async def test_registry_total_timeout_does_not_wait_for_probe_cancellation() -> None:
    registry = ReadinessRegistry(timeout_seconds=0.01)
    registry.register("search", cancellation_resistant_check)

    started_at = perf_counter()
    assert await registry.check() == ["search"]
    elapsed = perf_counter() - started_at
    await asyncio.sleep(0.21)

    assert elapsed < 0.1


def test_ready_route_returns_200_when_all_checks_pass() -> None:
    registry = ReadinessRegistry()
    registry.register("configuration", passing_check)

    response = TestClient(create_app(readiness_registry=registry)).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_route_returns_safe_503_for_failed_checks() -> None:
    registry = ReadinessRegistry()
    registry.register("search", leaking_check)

    response = TestClient(create_app(readiness_registry=registry)).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failed": ["search"]}
    assert "secret credential text" not in response.text


def test_default_application_is_ready_and_liveness_stays_exact() -> None:
    client = TestClient(create_app())

    assert client.get("/health/ready").json() == {"status": "ready"}
    assert client.get("/health/live").json() == {"status": "ok"}
