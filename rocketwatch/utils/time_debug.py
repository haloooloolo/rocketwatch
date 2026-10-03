import functools
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any, cast

log = logging.getLogger("rocketwatch.time_debug")


def timed[F: Callable[..., Any]](func: F) -> F:
    """Measure and log the execution time of a sync or async function"""

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.time()
            result = await func(*args, **kwargs)
            log.debug(f"{func.__name__} took {time.time() - start} seconds")
            return result

        return cast(F, async_wrapper)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.time()
        result = func(*args, **kwargs)
        log.debug(f"{func.__name__} took {time.time() - start} seconds")
        return result

    return cast(F, wrapper)
