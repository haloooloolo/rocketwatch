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


def _seed_apr_datapoints(value_step: float = 0.0001) -> list[dict[str, Any]]:
    # The chart path needs `set_xlim(left=x_arr[38])` to be in range, which
    # means at least 39 entries. Generate 50 to comfortably cover both
    # branches of the i>8 7-day-average computation.
    base_time = 1_700_000_000
    return [
        {
            "block": 1000 + i,
            "time": base_time + i * 86400,
            "value": 1.0 + i * value_step,
            "effectiveness": 0.95,
        }
        for i in range(50)
    ]


async def _seed_minipools(mongo_db: AsyncDatabase[dict[str, Any]], n: int = 3) -> None:
    await mongo_db.minipools.insert_many(
        [
            {
                "beacon": {"status": "active_ongoing"},
                "node_fee": 0.14,
                "node_deposit_balance": 8.0,
            }
            for _ in range(n)
        ]
    )


class TestRethAprHappyPath:
    async def test_renders_chart_and_fields(
        self,
        cog: APR,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        await mongo_db.reth_apr.insert_many(_seed_apr_datapoints())
        await _seed_minipools(mongo_db)

        interaction = make_interaction()
        await cog.reth_apr.callback(cog, interaction)

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        embed = kwargs["embed"]
        assert embed.title == "Current rETH APR"
        # Headline fields show the 7-day claim.
        field_names = [f.name for f in embed.fields]
        assert any("Day Average rETH APR" in n for n in field_names)
        # Image attached.
        assert kwargs["file"].filename == "reth_apr.png"

    async def test_empty_minipools_falls_back_to_default_node_fee(
        self,
        cog: APR,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        # No minipools ⇒ aggregation returns nothing, cog uses default 20.
        await mongo_db.reth_apr.insert_many(_seed_apr_datapoints())
        interaction = make_interaction()
        await cog.reth_apr.callback(cog, interaction)

        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.call_args.kwargs["embed"]
        # The "Current Average Effective Commission" field renders the default.
        commission_field = next(
            f for f in embed.fields if "Effective Commission" in f.name
        )
        assert "2000.00%" in commission_field.value


class TestNodeAprHappyPath:
    async def test_renders_chart_and_fields(
        self,
        cog: APR,
        scripted_rp: ScriptedRocketPool,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        # node_apr fetches the LEB4 commission via the network-settings contract.
        # solidity.to_float treats 14 * 10**16 as 0.14.
        scripted_rp.set_call(
            "rocketDAOProtocolSettingsNetwork.getNodeShare", 14 * 10**16
        )
        await mongo_db.reth_apr.insert_many(_seed_apr_datapoints())
        await _seed_minipools(mongo_db)

        interaction = make_interaction()
        await cog.node_apr.callback(cog, interaction)

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        embed = kwargs["embed"]
        assert embed.title == "Current NO APR"
        # The leb-4 / leb-8 split lands in the description embed field.
        field_text = " ".join(f.value for f in embed.fields)
        assert "leb4" in field_text
        assert "leb8" in field_text
        assert kwargs["file"].filename == "no_apr.png"
