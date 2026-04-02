import inspect
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from retry_async.api import EXCEPTIONS
from retry_async.api import retry as __retry

P = ParamSpec("P")
R = TypeVar("R")


def retry(
    exceptions: EXCEPTIONS = Exception,
    *,
    tries: int = -1,
    delay: float = 0,
    max_delay: float | None = None,
    backoff: float = 1,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        return __retry(  # type: ignore[no-any-return]
            exceptions,
            is_async=inspect.iscoroutinefunction(func),
            tries=tries,
            delay=delay,
            max_delay=max_delay,  # pyright: ignore[reportArgumentType]
            backoff=backoff,
        )(func)

    return decorator
