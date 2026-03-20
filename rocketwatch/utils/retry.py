import inspect
from collections.abc import Callable
from typing import Any

from retry_async.api import EXCEPTIONS
from retry_async.api import retry as __retry


def retry(
    exceptions: EXCEPTIONS = Exception,
    *,
    tries: int = -1,
    delay: float = 0,
    max_delay: float | None = None,
    backoff: float = 1,
) -> Callable[..., Any]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return __retry(
            exceptions,
            is_async=inspect.iscoroutinefunction(func),
            tries=tries,
            delay=delay,
            max_delay=max_delay,  # pyright: ignore[reportArgumentType]
            backoff=backoff,
        )(func)

    return decorator
