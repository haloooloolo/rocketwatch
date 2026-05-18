from typing import Any
from unittest.mock import MagicMock

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.apr.apr import (
    APR,
    APRDatapoint,
    get_duration,
    get_period_change,
    to_apr,
)
from tests.lib.discord_harness import make_bot, make_interaction, run_command
from tests.lib.scripted_rocketpool import ScriptedRocketPool

SECONDS_PER_YEAR = 365 * 24 * 60 * 60


def _dp(
    *, block: int, time: float, value: float, effectiveness: float = 1.0
) -> APRDatapoint:
    return {
        "block": block,
        "time": time,
        "value": value,
        "effectiveness": effectiveness,
    }


class TestAprMath:
    def test_to_apr_annualizes_period_change(self) -> None:
        # Half-year period, value went 1.0 → 1.05 ⇒ 10% APR (annualised).
        d1 = _dp(block=1, time=0, value=1.0)
        d2 = _dp(block=2, time=SECONDS_PER_YEAR / 2, value=1.05)
        assert to_apr(d1, d2) == pytest.approx(0.10, rel=1e-9)

    def test_get_period_change_pure_growth(self) -> None:
        d1 = _dp(block=1, time=0, value=100.0)
        d2 = _dp(block=2, time=86400, value=110.0)
        assert get_period_change(d1, d2) == pytest.approx(0.10, rel=1e-9)

    def test_get_period_change_virtual_divides_out_effectiveness(self) -> None:
        # effective=False divides by d2's effectiveness — if effectiveness=0.5
        # the virtual APR is double the observed.
        d1 = _dp(block=1, time=0, value=100.0)
        d2 = _dp(block=2, time=86400, value=110.0, effectiveness=0.5)
        observed = get_period_change(d1, d2, effective=True)
        virtual = get_period_change(d1, d2, effective=False)
        assert virtual == pytest.approx(observed / 0.5, rel=1e-9)

    def test_get_duration_returns_time_delta(self) -> None:
        d1 = _dp(block=1, time=100, value=1.0)
        d2 = _dp(block=2, time=250, value=1.0)
        assert get_duration(d1, d2) == 150


@pytest.fixture
def cog(
    mongo_db: AsyncDatabase[dict[str, Any]],
    scripted_rp: ScriptedRocketPool,
    monkeypatch: pytest.MonkeyPatch,
) -> APR:
    # APR.__init__ kicks off `tasks.loop`, which requires a running event loop
    # we don't have at construction time. Stub the start so we can build the cog.
    monkeypatch.setattr(
        "rocketwatch.plugins.apr.apr.APR.task",
        MagicMock(),
    )
    return APR(make_bot(db=mongo_db))


pytestmark = pytest.mark.integration_db


class TestRethAprEarlyReturn:
    async def test_empty_datapoints_short_circuits(self, cog: APR) -> None:
        # No data in reth_apr collection ⇒ the cog should send an embed with
        # "No data available yet." and NOT try to plot/multicall anything.
        interaction = make_interaction()
        embed = await run_command(cog, "reth_apr", interaction)
        assert embed.title == "Current rETH APR"
        assert embed.description == "No data available yet."

    async def test_node_apr_empty_datapoints_short_circuits(self, cog: APR) -> None:
        interaction = make_interaction()
        embed = await run_command(cog, "node_apr", interaction)
        assert embed.title == "Current NO APR"
        assert embed.description == "No data available yet."
