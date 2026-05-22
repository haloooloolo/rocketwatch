from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.tvl.tvl import (
    TVL,
    megapool_split_rewards,
    minipool_split_rewards_logic,
)
from rocketwatch.utils import shared_w3
from tests.lib.discord_harness import make_bot, make_interaction
from tests.lib.scripted_rocketpool import ScriptedRocketPool, addr

ETH = 10**18


class TestMinipoolSplitRewardsLogic:
    def test_below_base_threshold_skips_base_without_force(self) -> None:
        # balance < 8 and no force ⇒ no base allocation, everything is rewards.
        out = minipool_split_rewards_logic(balance=4.0, node_share=0.25, commission=0.1)
        assert out["base"] == {"reth": 0.0, "node": 0.0}
        # node_ownership_share = 0.25 + 0.75*0.1 = 0.325
        assert out["rewards"]["node"] == pytest.approx(4.0 * 0.325)
        assert out["rewards"]["reth"] == pytest.approx(4.0 * 0.675)

    def test_force_base_allocates_base_even_below_threshold(self) -> None:
        out = minipool_split_rewards_logic(
            balance=4.0, node_share=0.25, commission=0.1, force_base=True
        )
        # node_balance = 32*0.25 = 8, reth_balance = 24. Only 4 ETH available →
        # all goes to reth base (capped at reth_balance), node base gets 0,
        # nothing left for rewards.
        assert out["base"]["reth"] == pytest.approx(4.0)
        assert out["base"]["node"] == 0.0
        assert out["rewards"] == {"reth": 0.0, "node": 0.0}

    def test_full_32_eth_splits_base_then_rewards(self) -> None:
        # 36 ETH with node_share 0.25 (node_balance=8, reth_balance=24):
        # base: reth 24, node 8 → 32 consumed; 4 ETH rewards.
        out = minipool_split_rewards_logic(
            balance=36.0, node_share=0.25, commission=0.1
        )
        assert out["base"]["reth"] == pytest.approx(24.0)
        assert out["base"]["node"] == pytest.approx(8.0)
        # remaining 4 ETH split: node_ownership_share = 0.325
        assert out["rewards"]["node"] == pytest.approx(4.0 * 0.325)
        assert out["rewards"]["reth"] == pytest.approx(4.0 * 0.675)


class TestMegapoolSplitRewards:
    def test_splits_borrowed_portion_by_commission(self) -> None:
        # rewards=10, capital_ratio=0.25 (so borrowed portion = 7.5),
        # node_commission=0.05, voter=0.02, dao=0.01.
        out = megapool_split_rewards(
            rewards=10.0,
            capital_ratio=0.25,
            node_commission=0.05,
            voter_share=0.02,
            dao_share=0.01,
        )
        borrowed = 10.0 * 0.75
        assert out["reth"] == pytest.approx(borrowed * (1 - 0.05 - 0.02 - 0.01))
        assert out["voter"] == pytest.approx(borrowed * 0.02)
        assert out["dao"] == pytest.approx(borrowed * 0.01)
        # node gets everything not assigned to the other three.
        assert out["node"] == pytest.approx(
            10.0 - out["reth"] - out["voter"] - out["dao"]
        )
        # The four shares must sum back to the full reward.
        assert sum(out.values()) == pytest.approx(10.0)


@pytest.fixture
def _stub_tvl_externals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # rp.get_eth_usdc_price isn't part of ScriptedRocketPool; w3.eth.get_balance
    # needs to be awaitable. Provide deterministic stand-ins.
    async def eth_balance(_addr: Any, **_kwargs: Any) -> int:
        return 5 * ETH

    eth = shared_w3.w3._instance.eth  # type: ignore[union-attr]
    monkeypatch.setattr(eth, "get_balance", AsyncMock(side_effect=eth_balance))
    yield


def _seed_tvl_calls(scripted_rp: ScriptedRocketPool) -> None:
    scripted_rp.set_address("rocketTokenRPL", addr("0x" + "11" * 20))
    scripted_rp.set_address("rocketTokenRETH", addr("0x" + "22" * 20))
    scripted_rp.set_address("rocketSmoothingPool", addr("0x" + "33" * 20))
    calls = {
        "rocketNetworkPrices.getRPLPrice": 5 * 10**16,  # 0.05 ETH/RPL
        "rocketDepositPool.getBalance": 100 * ETH,
        "rocketNodeStaking.getTotalLegacyStakedRPL": 1000 * ETH,
        "rocketNodeStaking.getTotalMegapoolStakedRPL": 500 * ETH,
        "rocketDAOProtocolSettingsNetwork.getNodeShare": 5 * 10**16,
        "rocketDAOProtocolSettingsNetwork.getVoterShare": 2 * 10**16,
        "rocketDAOProtocolSettingsNetwork.getProtocolDAOShare": 1 * 10**16,
    }
    for k, v in calls.items():
        scripted_rp.set_call(k, v)
    # rocketVault.balanceOf / balanceOfToken are called with various args;
    # return a flat value for any.
    scripted_rp.set_call("rocketVault.balanceOf", lambda *_: 10 * ETH)
    scripted_rp.set_call("rocketVault.balanceOfToken", lambda *_: 20 * ETH)


@pytest.fixture
def _stub_eth_usdc(
    monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
) -> None:
    monkeypatch.setattr(
        scripted_rp, "get_eth_usdc_price", AsyncMock(return_value=3000.0), raising=False
    )


class TestTvlCommand:
    async def test_empty_db_renders_zero_tree(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        _stub_tvl_externals: None,
        _stub_eth_usdc: None,
    ) -> None:
        # No minipools / megapools / node_operators: every aggregation is empty,
        # so only the live rp.call values contribute. Exercises the full
        # set_val_of_branch rollup + render path.
        _seed_tvl_calls(scripted_rp)

        cog = TVL(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.tvl.callback(cog, interaction)

        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.title == "Protocol TVL"
        assert embed.description is not None
        # render_tree uses non-breaking spaces; assert on stable tokens.
        assert "USDC" in embed.description
        assert "ETH" in embed.description

    async def test_minipool_and_megapool_data_flows_into_tree(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        _stub_tvl_externals: None,
        _stub_eth_usdc: None,
    ) -> None:
        _seed_tvl_calls(scripted_rp)
        # A staking minipool with beacon rewards above 32 ETH.
        await mongo_db.minipools.insert_one(
            {
                "status": "staking",
                "node_deposit_balance": 8.0,
                "node_fee": 0.1,
                "node_refund_balance": 0.0,
                "execution_balance": 0.5,
                "beacon": {"balance": 34.0},
            }
        )
        # A dissolved minipool (separate aggregation branch).
        await mongo_db.minipools.insert_one(
            {
                "status": "dissolved",
                "vacant": False,
                "beacon": {"balance": 1.0},
                "execution_balance": 0.0,
            }
        )
        # A staking megapool validator with rewards.
        await mongo_db.megapool_validators.insert_one(
            {
                "status": "staking",
                "requested_bond": 8.0,
                "beacon": {"balance": 34.0},
            }
        )

        cog = TVL(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.tvl.callback(cog, interaction)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.title == "Protocol TVL"
        # Tree should show non-zero ETH somewhere.
        assert "ETH" in (embed.description or "")

    async def test_refund_and_megapool_balance_branches(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        _stub_tvl_externals: None,
        _stub_eth_usdc: None,
    ) -> None:
        _seed_tvl_calls(scripted_rp)
        # Minipool with a refund balance larger than its contract balance, so
        # the refund spills over into the beacon balance (lines 194-220).
        await mongo_db.minipools.insert_one(
            {
                "status": "staking",
                "node_deposit_balance": 8.0,
                "node_fee": 0.1,
                "node_refund_balance": 2.0,
                "execution_balance": 0.5,
                "beacon": {"balance": 33.0},
            }
        )
        # Prestaked + dissolved megapool validators (lines 271, 290).
        await mongo_db.megapool_validators.insert_many(
            [
                {"status": "prestaked"},
                {"status": "dissolved", "beacon": {"balance": 1.0}},
                # Exiting validator penalised below 32 (lines 308-309).
                {
                    "status": "exiting",
                    "requested_bond": 8.0,
                    "beacon": {"balance": 30.0},
                },
            ]
        )
        # Node operator with a deployed megapool carrying refund + rewards
        # (lines 365-391) and a fee distributor balance (lines 533-536).
        await mongo_db.node_operators.insert_one(
            {
                "megapool": {
                    "deployed": True,
                    "eth_balance": 3.0,
                    "refund_value": 1.0,
                    "debt": 0.2,
                    "pending_rewards": 1.5,
                    "node_bond": 8.0,
                    "user_capital": 24.0,
                },
                "fee_distributor": {"eth_balance": 2.0},
                "effective_node_share": 0.25,
                "average_node_fee": 0.1,
            }
        )

        cog = TVL(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.tvl.callback(cog, interaction)

        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.title == "Protocol TVL"

    async def test_show_all_uses_full_depth(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        _stub_tvl_externals: None,
        _stub_eth_usdc: None,
    ) -> None:
        _seed_tvl_calls(scripted_rp)
        cog = TVL(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.tvl.callback(cog, interaction, show_all=True)

        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.call_args.kwargs["embed"]
        # show_all renders deeper leaves like the rETH/Node Share breakdown.
        assert "Share" in (embed.description or "")
