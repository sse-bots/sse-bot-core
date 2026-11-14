"""Reusable startup retry helper for SSE bot runtime-lease contention.

Place this file inside the shared ``sse_bot_core`` package as
``sse_bot_core/runtime_retry.py`` and import ``retry_runtime_lease`` from it.

This helper retries ONLY errors that clearly indicate runtime-lease contention.
It never deletes, steals, or bypasses a lease. A live second process must be
stopped; otherwise the bounded retries will finish and the original error will
be raised.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

# Keep the match narrow so unrelated startup/database/network errors are not
# mistaken for lease contention.
_LEASE_ERROR_MARKERS = (
    "already owns the runtime lease",
    "runtime lease is already owned",
    "runtime lease is held by another",
    "another process already owns the runtime lease",
)


def _is_runtime_lease_contention(error: BaseException) -> bool:
    message = str(error).casefold()
    return any(marker in message for marker in _LEASE_ERROR_MARKERS)


async def retry_runtime_lease(
    operation: Callable[..., Awaitable[T]],
    *args: Any,
    logger: logging.Logger | None = None,
    max_attempts: int = 8,
    initial_delay: float = 5.0,
    max_delay: float = 30.0,
    jitter: float = 0.20,
    **kwargs: Any,
) -> T:
    """Run an async startup operation, retrying temporary lease contention.

    Delays grow exponentially (5, 10, 20, then at most 30 seconds by default)
    with small random jitter. After ``max_attempts`` the final exception is
    re-raised. Other exceptions are raised immediately.

    Example::

        await retry_runtime_lease(setup_activation, self, logger=log)
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if initial_delay < 0 or max_delay < 0 or jitter < 0:
        raise ValueError("retry delays and jitter cannot be negative")

    log = logger or logging.getLogger("sse-runtime-retry")

    for attempt in range(1, max_attempts + 1):
        try:
            return await operation(*args, **kwargs)
        except Exception as error:
            if not _is_runtime_lease_contention(error):
                raise

            if attempt >= max_attempts:
                log.critical(
                    "Runtime lease is still owned by another process after %s attempts. "
                    "Startup is stopping safely. Check Render for another running "
                    "service/instance or an overlapping deployment. Original error: %s",
                    max_attempts,
                    error,
                )
                raise

            delay = min(initial_delay * (2 ** (attempt - 1)), max_delay)
            if jitter:
                delay *= 1 + random.uniform(-jitter, jitter)
            delay = max(0.0, delay)

            log.warning(
                "Runtime lease is temporarily unavailable (attempt %s/%s). "
                "Retrying startup in %.1f seconds. If another bot process is "
                "actually running, stop that process; this retry will not bypass it. Error: %s",
                attempt,
                max_attempts,
                delay,
                error,
            )
            await asyncio.sleep(delay)

    # Defensive fallback; the loop always returns or raises.
    raise RuntimeError("Runtime lease retry loop exited unexpectedly")
