from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable


def retry_delay(attempt: int, retry_after: float | None = None, *, cap: float = 60.0) -> float:
    if retry_after is not None:
        return min(cap, max(0.0, retry_after))
    base = min(cap, float(2 ** max(0, attempt - 1)))
    return min(cap, base * (0.8 + random.random() * 0.4))


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 5,
    retryable: Callable[[Exception], bool],
    retry_after: Callable[[Exception], float | None] = lambda error: None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as error:
            if attempt == attempts or not retryable(error):
                raise
            await sleep(retry_delay(attempt, retry_after(error)))
    raise RuntimeError("unreachable")