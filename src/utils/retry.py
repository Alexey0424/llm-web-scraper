"""Retry with exponential backoff (deterministic, no random jitter)."""

from __future__ import annotations

import functools
import time
from typing import Callable, Iterable, Type


def with_retry(
    max_retries: int = 3,
    base: float = 1.5,
    exceptions: Iterable[Type[BaseException]] = (Exception,),
    sleep_fn: Callable[[float], None] = time.sleep,
    should_retry: Callable[[BaseException], bool] | None = None,
):
    """Decorator: retry the wrapped callable with exponential backoff.

    Delay before attempt *n* (1-indexed retries) is ``base ** n`` seconds.
    ``should_retry(exc)`` can veto the retry for failures that cannot succeed
    on a repeat (e.g. an HTTP 4xx) — those re-raise immediately.
    """

    exc_tuple = tuple(exceptions)

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except exc_tuple as exc:  # noqa: PERF203
                    last_exc = exc
                    if attempt == max_retries or (
                            should_retry is not None and not should_retry(exc)):
                        break
                    sleep_fn(base ** (attempt + 1))
            raise last_exc

        return wrapper

    return decorator
