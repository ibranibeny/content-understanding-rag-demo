import asyncio
from collections.abc import Mapping

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.readiness import ReadinessRegistry
from app.domain.protocols import ReadinessCheck
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


async def test_registry_total_timeout_does_not_wait_for_probe_cancellation() -> None:
    timeout_seconds = 0.01
    cancellation_received = asyncio.Event()
    release_probe = asyncio.Event()
    probe_finished = asyncio.Event()

    async def cancellation_resistant_check() -> bool:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_received.set()
            await release_probe.wait()
        finally:
            probe_finished.set()
        return True

    registry = ReadinessRegistry(timeout_seconds=timeout_seconds)
    registry.register("search", cancellation_resistant_check)

    assert await registry.check() == ["search"]
    await asyncio.wait_for(cancellation_received.wait(), timeout=1.0)
    assert not probe_finished.is_set()

    release_probe.set()
    await probe_finished.wait()


async def test_cancelling_registry_check_cleans_up_spawned_probe_tasks() -> None:
    probe_started = asyncio.Event()
    cancellation_received = asyncio.Event()
    release_probe = asyncio.Event()
    probe_finished = asyncio.Event()
    loop = asyncio.get_running_loop()
    unhandled_contexts: list[dict[str, object]] = []
    previous_exception_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled_contexts.append(context))

    async def cancellation_resistant_check() -> bool:
        probe_started.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        except asyncio.CancelledError:
            cancellation_received.set()
            await release_probe.wait()
            raise RuntimeError("probe failed during cancellation")
        finally:
            probe_finished.set()

    registry = ReadinessRegistry(timeout_seconds=2.0)
    registry.register("resistant", cancellation_resistant_check)
    registry_task = asyncio.create_task(registry.check())

    try:
        await probe_started.wait()
        registry_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await registry_task

        await cancellation_received.wait()
        release_probe.set()
        await probe_finished.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not [
            task
            for task in asyncio.all_tasks()
            if task.get_name().startswith("readiness:")
        ]
        assert unhandled_contexts == []
    finally:
        release_probe.set()
        loop.set_exception_handler(previous_exception_handler)


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


def test_ready_route_returns_200_when_all_checks_pass() -> None:
    response = TestClient(
        create_app(readiness_checks={"configuration": passing_check})
    ).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_route_returns_safe_503_for_failed_checks() -> None:
    response = TestClient(create_app(readiness_checks={"search": leaking_check})).get(
        "/health/ready"
    )

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failed": ["search"]}
    assert "secret credential text" not in response.text


def test_default_application_is_ready_and_liveness_stays_exact() -> None:
    client = TestClient(create_app())

    assert client.get("/health/ready").json() == {"status": "ready"}
    assert client.get("/health/live").json() == {"status": "ok"}


def test_test_mode_defaults_to_configuration_only_readiness() -> None:
    app = create_app(settings=Settings(app_mode="test"))

    assert TestClient(app).get("/health/ready").json() == {"status": "ready"}


def test_production_without_injected_checks_fails_closed_for_every_dependency() -> None:
    settings = Settings(
        app_mode="production", frontend_origin="https://frontend.example.com"
    )
    client = TestClient(create_app(settings=settings))

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "failed": ["blob", "foundry", "queue", "search", "table"],
    }
    assert client.get("/health/live").json() == {"status": "ok"}


def test_production_accepts_exact_named_dependency_checks() -> None:
    checks = {
        "blob": passing_check,
        "queue": passing_check,
        "table": passing_check,
        "search": passing_check,
        "foundry": passing_check,
    }

    response = TestClient(
        create_app(
            settings=Settings(
                app_mode="production", frontend_origin="https://frontend.example.com"
            ),
            readiness_checks=checks,
        )
    ).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.parametrize(
    "checks",
    [
        {},
        {"blob": passing_check},
        {
            "blob": passing_check,
            "queue": passing_check,
            "table": passing_check,
            "search": passing_check,
            "foundry": passing_check,
            "configuration": passing_check,
        },
    ],
)
def test_production_rejects_incomplete_or_extra_dependency_checks(
    checks: Mapping[str, ReadinessCheck],
) -> None:
    with pytest.raises(ValueError, match="blob.*foundry.*queue.*search.*table"):
        create_app(
            settings=Settings(
                app_mode="production", frontend_origin="https://frontend.example.com"
            ),
            readiness_checks=checks,
        )
