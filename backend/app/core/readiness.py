import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

ReadinessCallable = Callable[[], Coroutine[Any, Any, bool]]


class ReadinessRegistry:
    """Runs named dependency probes within one shared timeout budget."""

    def __init__(self, timeout_seconds: float = 2.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._checks: dict[str, ReadinessCallable] = {}

    def register(self, name: str, check: ReadinessCallable) -> None:
        if not name:
            raise ValueError("readiness check name must not be empty")
        if name in self._checks:
            raise ValueError(f"readiness check already registered: {name}")
        self._checks[name] = check

    async def check(self) -> list[str]:
        tasks: dict[str, asyncio.Task[bool]] = {
            name: asyncio.create_task(check(), name=f"readiness:{name}")
            for name, check in self._checks.items()
        }
        if not tasks:
            return []

        try:
            done, pending = await asyncio.wait(
                tasks.values(), timeout=self._timeout_seconds, return_when=asyncio.ALL_COMPLETED
            )
            return sorted(
                name
                for name, task in tasks.items()
                if task in pending or (task in done and self._task_failed(task))
            )
        finally:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
                task.add_done_callback(self._consume_task_result)

    @staticmethod
    def _task_failed(task: asyncio.Task[bool]) -> bool:
        if task.cancelled():
            return True
        return task.exception() is not None or not task.result()

    @staticmethod
    def _consume_task_result(task: asyncio.Task[bool]) -> None:
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
