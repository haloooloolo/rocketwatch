import asyncio
import inspect
import logging

import pytest

from rocketwatch.utils.time_debug import timed


class TestTimed:
    def test_sync_function_returns_value_and_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        @timed
        def add(a: int, b: int) -> int:
            return a + b

        with caplog.at_level(logging.DEBUG, logger="rocketwatch.time_debug"):
            assert add(2, 3) == 5
        assert "add took" in caplog.text

    async def test_async_function_stays_awaitable_and_times_the_await(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        @timed
        async def slow() -> str:
            await asyncio.sleep(0.05)
            return "done"

        assert inspect.iscoroutinefunction(slow)
        with caplog.at_level(logging.DEBUG, logger="rocketwatch.time_debug"):
            assert await slow() == "done"
        [record] = [r for r in caplog.records if "slow took" in r.message]
        assert float(record.message.split()[2]) >= 0.05
