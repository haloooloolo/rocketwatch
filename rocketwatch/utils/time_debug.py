import functools
import logging
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger("rocketwatch.time_debug")


def timerun[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Measure and log the execution time of a method"""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        start = time.time()
        result = func(*args, **kwargs)
        duration = time.time() - start

        log.debug(f"{func.__name__} took {duration} seconds")
        return result

    return wrapper


def timerun_async[**P, R](func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Measure and log the execution time of an async method"""

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        start = time.time()
        result = await func(*args, **kwargs)
        duration = time.time() - start

        log.debug(f"{func.__name__} took {duration} seconds")
        return result

    return wrapper
