import asyncio
import logging
from typing import Any

from cronitor import Monitor

from rocketwatch.utils.config import cfg

log = logging.getLogger("rocketwatch.cronitor_monitor")


class AsyncMonitor:
    """Cronitor monitor whose blocking HTTP ping runs off the event loop."""

    def __init__(self, key: str) -> None:
        api_key = cfg.secrets.cronitor
        self._monitor = Monitor(key, api_key=api_key) if api_key else None

    async def ping(self, **params: Any) -> None:
        if self._monitor is None:
            return
        try:
            await asyncio.to_thread(self._monitor.ping, **params)
        except Exception as err:
            log.warning(f"Cronitor ping for {self._monitor.key} failed: {err}")
