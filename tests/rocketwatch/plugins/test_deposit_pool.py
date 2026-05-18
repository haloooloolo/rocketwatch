from unittest.mock import AsyncMock

import pytest

from rocketwatch.plugins.deposit_pool.deposit_pool import DepositPool
from tests.lib.discord_harness import make_bot, make_interaction, run_command
from tests.lib.scripted_rocketpool import ScriptedRocketPool

ETH = 10**18


def _set_pool_calls(
    scripted_rp: ScriptedRocketPool,
    *,
    balance_eth: float,
    cap_eth: int,
    free_eth: float,
) -> None:
    scripted_rp.set_call("rocketDepositPool.getBalance", int(balance_eth * ETH))
    scripted_rp.set_call(
        "rocketDAOProtocolSettingsDeposit.getMaximumDepositPoolSize", cap_eth * ETH
    )
    scripted_rp.set_call(
        "rocketDepositPool.getMaximumDepositAmount", int(free_eth * ETH)
    )


@pytest.fixture
def cog(
    scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
) -> DepositPool:
    # The deposit_pool embed embeds queue summaries from `Queue.get_*_queue`.
    # Default to empty queues; tests that need queue content override these.
    monkeypatch.setattr(
        "rocketwatch.plugins.deposit_pool.deposit_pool.Queue.get_express_queue",
        AsyncMock(return_value=(0, "")),
    )
    monkeypatch.setattr(
        "rocketwatch.plugins.deposit_pool.deposit_pool.Queue.get_standard_queue",
        AsyncMock(return_value=(0, "")),
    )
    return DepositPool(make_bot())


class TestDepositPoolStats:
    async def test_renders_size_and_capacity_fields(
        self, cog: DepositPool, scripted_rp: ScriptedRocketPool
    ) -> None:
        _set_pool_calls(scripted_rp, balance_eth=100, cap_eth=320, free_eth=220)
        embed = await run_command(cog, "deposit_pool", make_interaction())

        assert embed.title == "Deposit Pool Stats"
        fields = {f.name: f.value for f in embed.fields}
        assert fields["Current Size"] == "100.00 ETH"
        assert fields["Maximum Size"] == "320 ETH"
        # 320 - 100 = 220 ETH free; the cog rounds via :,.2f.
        assert "220.00 ETH" in fields["Status"]

    async def test_status_says_capacity_reached_when_full(
        self, cog: DepositPool, scripted_rp: ScriptedRocketPool
    ) -> None:
        # cap - balance < 0.01 ⇒ "Capacity reached!".
        _set_pool_calls(scripted_rp, balance_eth=320, cap_eth=320, free_eth=0)
        embed = await run_command(cog, "deposit_pool", make_interaction())
        assert "Capacity reached!" in {f.value for f in embed.fields}

    async def test_enough_for_counts_eb4_and_credit_validators(
        self, cog: DepositPool, scripted_rp: ScriptedRocketPool
    ) -> None:
        # 64 ETH in pool, no queue: enough for 2 EB4 (28 ETH/validator) and 2
        # credit (32 ETH/validator) — both lines should show.
        _set_pool_calls(scripted_rp, balance_eth=64, cap_eth=320, free_eth=256)
        embed = await run_command(cog, "deposit_pool", make_interaction())

        fields = {f.name: f.value for f in embed.fields}
        assert "Enough For" in fields
        assert "2" in fields["Enough For"]
        assert "4 ETH validators" in fields["Enough For"]
        assert "credit validators" in fields["Enough For"]

    async def test_queue_summary_replaces_enough_for_block(
        self,
        cog: DepositPool,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When the queue is non-empty, the description holds queue content
        # instead of an "Enough For" field.
        monkeypatch.setattr(
            "rocketwatch.plugins.deposit_pool.deposit_pool.Queue.get_express_queue",
            AsyncMock(return_value=(3, "1. node-a\n2. node-b\n")),
        )
        monkeypatch.setattr(
            "rocketwatch.plugins.deposit_pool.deposit_pool.Queue.get_standard_queue",
            AsyncMock(return_value=(1, "1. node-c\n")),
        )
        _set_pool_calls(scripted_rp, balance_eth=100, cap_eth=320, free_eth=220)
        embed = await run_command(cog, "deposit_pool", make_interaction())

        assert embed.description is not None
        assert "Express Queue (3)" in embed.description
        assert "Standard Queue (1)" in embed.description
        # 100 ETH // 32 = 3 possible assignments; 4 in queue ⇒ min(3,4) = 3.
        assert "3 deposit assignments" in embed.description
        # The "Enough For" branch should NOT fire when a queue exists.
        assert "Enough For" not in {f.name for f in embed.fields}


class TestRethExtraCollateral:
    async def _set_reth_calls(
        self,
        scripted_rp: ScriptedRocketPool,
        *,
        exchange_rate: int,
        total_supply: int,
        collateral_rate: int,
        target_rate: int,
    ) -> None:
        scripted_rp.set_call("rocketTokenRETH.getExchangeRate", exchange_rate)
        scripted_rp.set_call("rocketTokenRETH.totalSupply", total_supply)
        scripted_rp.set_call("rocketTokenRETH.getCollateralRate", collateral_rate)
        scripted_rp.set_call(
            "rocketDAOProtocolSettingsNetwork.getTargetRethCollateralRate", target_rate
        )

    async def test_describes_collateral_percentage(
        self, cog: DepositPool, scripted_rp: ScriptedRocketPool
    ) -> None:
        # supply=1000 rETH, rate=1.0 → 1000 ETH backing; collateral_rate=5% →
        # 50 ETH; target_rate=10% → 100 ETH; ratio = 50%.
        await self._set_reth_calls(
            scripted_rp,
            exchange_rate=ETH,
            total_supply=1000 * ETH,
            collateral_rate=5 * 10**16,
            target_rate=10 * 10**16,
        )
        embed = await run_command(cog, "reth_extra_collateral", make_interaction())
        assert embed.title == "rETH Extra Collateral"
        assert embed.description is not None
        assert "50.00 ETH" in embed.description
        assert "50.00%" in embed.description
        assert "100 ETH target" in embed.description

    async def test_zero_collateral_branches_to_no_liquidity(
        self, cog: DepositPool, scripted_rp: ScriptedRocketPool
    ) -> None:
        await self._set_reth_calls(
            scripted_rp,
            exchange_rate=ETH,
            total_supply=1000 * ETH,
            collateral_rate=0,
            target_rate=10 * 10**16,
        )
        embed = await run_command(cog, "reth_extra_collateral", make_interaction())
        assert embed.description is not None
        assert "No liquidity" in embed.description
        assert "100 ETH (10% of supply)" in embed.description
