import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from rocketwatch.utils import cronitor_monitor
from rocketwatch.utils.config import cfg
from rocketwatch.utils.cronitor_monitor import AsyncMonitor


@pytest.fixture
def inner(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(cfg.secrets, "cronitor", "test-key")
    monitor = MagicMock()
    monitor.key = "test-task"
    monkeypatch.setattr(cronitor_monitor, "Monitor", lambda *_, **__: monitor)
    return monitor


class TestAsyncMonitor:
    async def test_ping_forwards_params_off_the_loop_thread(
        self, inner: MagicMock
    ) -> None:
        threads: list[threading.Thread] = []

        def ping(**params: Any) -> None:
            threads.append(threading.current_thread())

        inner.ping.side_effect = ping
        await AsyncMonitor("test-task").ping(state="run", series=1.0)

        inner.ping.assert_called_once_with(state="run", series=1.0)
        assert threads[0] is not threading.main_thread()

    async def test_ping_failure_does_not_propagate(self, inner: MagicMock) -> None:
        inner.ping.side_effect = ConnectionError("cronitor unreachable")
        await AsyncMonitor("test-task").ping(state="complete")

    async def test_ping_is_skipped_without_api_key(
        self, inner: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cfg.secrets, "cronitor", "")
        await AsyncMonitor("test-task").ping(state="run")

        inner.ping.assert_not_called()
