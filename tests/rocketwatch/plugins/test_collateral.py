from collections.abc import Iterator
from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.collateral.collateral import (
    Collateral,
    collateral_distribution_raw,
    get_average_collateral_percentage_per_node,
    get_node_collateral_data,
    get_percentiles,
)
from tests.lib.discord_harness import make_bot, make_interaction
from tests.lib.scripted_rocketpool import ScriptedRocketPool


class TestGetPercentiles:
    def test_returns_one_pair_per_requested_percentile(self) -> None:
        ps = get_percentiles([10, 50, 90], [1.0, 2.0, 3.0, 4.0, 5.0])
        assert [p for p, _ in ps] == [10, 50, 90]

    def test_median_picks_actual_data_point(self) -> None:
        # method="nearest" guarantees the returned value is one of the inputs,
        # not interpolated — pin this since downstream rendering counts on it.
        _, median = next(p for p in get_percentiles([50], [1.0, 2.0, 3.0, 4.0, 5.0]))
        assert median in {2.0, 3.0}


pytestmark = pytest.mark.integration_db


class TestGetNodeCollateralData:
    async def test_includes_minipool_operators(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        # Operator with 2 staking minipools, 50% effective share.
        # bonded = 0.5 * 2 * 32 = 32 ETH (plus megapool bond=0)
        # borrowed = (1-0.5) * 2 * 32 = 32 ETH (plus user_capital=0)
        await mongo_db.node_operators.insert_one(
            {
                "address": "0xNODE1",
                "rpl": {"total_stake": 100.0},
                "effective_node_share": 0.5,
                "staking_minipool_count": 2,
                "megapool": {
                    "node_bond": 0,
                    "user_capital": 0,
                    "active_validator_count": 0,
                },
            }
        )
        data = await get_node_collateral_data(mongo_db)
        assert "0xNODE1" in data
        node = data["0xNODE1"]
        assert node["bonded"] == 32.0
        assert node["borrowed"] == 32.0
        assert node["rpl_stake"] == 100.0
        assert node["validators"] == 2

    async def test_includes_megapool_operators(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        # Megapool-only operator (no staking_minipool_count). Bonded/borrowed
        # come from the megapool subdocument.
        await mongo_db.node_operators.insert_one(
            {
                "address": "0xNODE2",
                "rpl": {"total_stake": 50.0},
                "effective_node_share": 0,
                "staking_minipool_count": 0,
                "megapool": {
                    "node_bond": 12,
                    "user_capital": 20,
                    "active_validator_count": 1,
                },
            }
        )
        data = await get_node_collateral_data(mongo_db)
        node = data["0xNODE2"]
        assert node["bonded"] == 12
        assert node["borrowed"] == 20
        assert node["validators"] == 1

    async def test_filters_out_inactive_operators(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        # No minipools AND no megapool validators ⇒ should be omitted from
        # the $match stage entirely.
        await mongo_db.node_operators.insert_one(
            {
                "address": "0xINACTIVE",
                "rpl": {"total_stake": 999.0},
                "staking_minipool_count": 0,
                "megapool": {"active_validator_count": 0},
            }
        )
        data = await get_node_collateral_data(mongo_db)
        assert "0xINACTIVE" not in data


class TestAverageCollateralPercentage:
    async def test_buckets_collateral_into_step_sized_groups(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        # Two operators at borrowed=32 ETH, with different RPL stakes.
        # rpl_price = 0.05 ETH per RPL.
        # node A: rpl=200 → effective = 10 ETH → collateral = 10/32 *100 = 31.25%
        # node B: rpl=400 → effective = 20 ETH → collateral = 62.50%
        await mongo_db.node_operators.insert_many(
            [
                {
                    "address": "0xA",
                    "rpl": {"total_stake": 200.0},
                    "effective_node_share": 0.5,
                    "staking_minipool_count": 2,
                    "megapool": {
                        "node_bond": 0,
                        "user_capital": 0,
                        "active_validator_count": 0,
                    },
                },
                {
                    "address": "0xB",
                    "rpl": {"total_stake": 400.0},
                    "effective_node_share": 0.5,
                    "staking_minipool_count": 2,
                    "megapool": {
                        "node_bond": 0,
                        "user_capital": 0,
                        "active_validator_count": 0,
                    },
                },
            ]
        )
        # 0.05 ETH per RPL → 5e16 with 18 decimals.
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", 5 * 10**16)

        result = await get_average_collateral_percentage_per_node(
            mongo_db, collateral_cap=None, bonded=False
        )
        # Two distinct collateral percentages produce two buckets.
        assert len(result) == 2
        # Each operator's full rpl_stake should land in its own bucket.
        all_rpl = [rpl for bucket in result.values() for rpl in bucket]
        assert sorted(all_rpl) == [200.0, 400.0]

    async def test_collateral_cap_clamps_overshooters(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        # One whale, RPL much larger than collateral_cap. With cap=50, the
        # capped collateral should land in the 50% bucket regardless of how
        # outsized the stake was.
        await mongo_db.node_operators.insert_one(
            {
                "address": "0xWHALE",
                "rpl": {"total_stake": 100_000.0},
                "effective_node_share": 0.5,
                "staking_minipool_count": 2,
                "megapool": {
                    "node_bond": 0,
                    "user_capital": 0,
                    "active_validator_count": 0,
                },
            }
        )
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", 5 * 10**16)
        result = await get_average_collateral_percentage_per_node(
            mongo_db, collateral_cap=50, bonded=False
        )
        # All buckets must be at or below 50 — nothing exceeds the cap.
        assert max(result.keys()) <= 50


@pytest.fixture
def _stub_resolve_ens(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def fake_resolve_ens(_interaction: Any, node_address: str) -> tuple[Any, Any]:
        # Pass the address through unchanged — strip any ENS-like suffix.
        # Tests use bare addresses so this just echoes them back.
        return node_address, node_address

    monkeypatch.setattr(
        "rocketwatch.plugins.collateral.collateral.resolve_ens", fake_resolve_ens
    )
    yield


async def _insert_basic_node(
    mongo_db: AsyncDatabase[dict[str, Any]],
    *,
    address: str,
    rpl: float = 100.0,
    minipools: int = 2,
) -> None:
    await mongo_db.node_operators.insert_one(
        {
            "address": address,
            "rpl": {"total_stake": rpl},
            "effective_node_share": 0.5,
            "staking_minipool_count": minipools,
            "megapool": {
                "node_bond": 0,
                "user_capital": 0,
                "active_validator_count": 0,
            },
        }
    )


class TestCollateralDistributionRaw:
    async def test_renders_distribution_block(self) -> None:
        # collateral_distribution_raw is the `raw=True` text-mode branch.
        interaction = make_interaction()
        await collateral_distribution_raw(interaction, [(10, 5), (20, 1), (30, 12)])
        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.description is not None
        # Singular/plural switch on count == 1.
        assert "1 node" in embed.description
        assert "5 nodes" in embed.description
        assert "12 nodes" in embed.description


class TestCollateralDistributionCommand:
    async def test_image_command_runs_end_to_end(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        await _insert_basic_node(mongo_db, address="0xA", rpl=200.0)
        await _insert_basic_node(mongo_db, address="0xB", rpl=400.0)
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", 5 * 10**16)

        cog = Collateral(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.collateral_distribution.callback(cog, interaction)

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        embed = kwargs["embed"]
        assert embed.title == "RPL Collateral Distribution"
        # Footer holds the percentile breakdown.
        assert embed.footer.text is not None
        assert "th percentile" in embed.footer.text
        # The image is delivered as an attached file.
        assert len(kwargs["files"]) == 1
        assert kwargs["files"][0].filename == "collateral_distribution.png"

    async def test_raw_mode_skips_image_path(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        await _insert_basic_node(mongo_db, address="0xA", rpl=200.0)
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", 5 * 10**16)

        cog = Collateral(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.collateral_distribution.callback(cog, interaction, raw=True)

        # raw=True takes the text-only path: no `files` kwarg, just an embed.
        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        assert "files" not in kwargs
        assert kwargs["embed"].description is not None
        assert "node" in kwargs["embed"].description


class TestNodeTvlVsCollateralCommand:
    async def test_runs_without_highlight(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        await _insert_basic_node(mongo_db, address="0xA", rpl=200.0, minipools=2)
        await _insert_basic_node(mongo_db, address="0xB", rpl=400.0, minipools=4)
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", 5 * 10**16)

        cog = Collateral(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.node_tvl_vs_collateral.callback(cog, interaction)

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        embed = kwargs["embed"]
        assert embed.title == "Node TVL vs Collateral Scatter Plot"
        assert kwargs["files"][0].filename == "graph.png"

    async def test_highlights_target_node(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        _stub_resolve_ens: None,
    ) -> None:
        await _insert_basic_node(mongo_db, address="0xTARGET", rpl=200.0, minipools=2)
        await _insert_basic_node(mongo_db, address="0xOTHER", rpl=400.0, minipools=4)
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", 5 * 10**16)

        cog = Collateral(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.node_tvl_vs_collateral.callback(
            cog, interaction, node_address="0xTARGET"
        )

        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.description is not None
        assert "Showing location of 0xTARGET" in embed.description

    async def test_unknown_target_reports_and_returns(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        _stub_resolve_ens: None,
    ) -> None:
        await _insert_basic_node(mongo_db, address="0xA", rpl=200.0)
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", 5 * 10**16)

        cog = Collateral(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.node_tvl_vs_collateral.callback(
            cog, interaction, node_address="0xMISSING"
        )

        # The "node not found" branch sends a plain text message and bails.
        interaction.followup.send.assert_awaited_once()
        args, kwargs = interaction.followup.send.call_args
        msg = args[0] if args else kwargs.get("content", "")
        assert "not found in data set" in msg

    async def test_resolve_ens_failure_short_circuits(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def failing_resolve(
            _interaction: Any, _node_address: str
        ) -> tuple[None, None]:
            return None, None

        monkeypatch.setattr(
            "rocketwatch.plugins.collateral.collateral.resolve_ens", failing_resolve
        )

        cog = Collateral(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.node_tvl_vs_collateral.callback(
            cog, interaction, node_address="bad.eth"
        )
        # resolve_ens returns None, None → command returns without sending.
        interaction.followup.send.assert_not_awaited()


class TestVoterShareDistributionCommand:
    async def test_renders_image_when_megapool_data_present(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.node_operators.insert_many(
            [
                {
                    "address": "0xA",
                    "rpl": {"megapool_stake": 100.0},
                    "megapool": {
                        "user_capital": 8.0,
                        "active_validator_count": 1,
                    },
                },
                {
                    "address": "0xB",
                    "rpl": {"megapool_stake": 250.0},
                    "megapool": {
                        "user_capital": 16.0,
                        "active_validator_count": 2,
                    },
                },
            ]
        )

        cog = Collateral(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.voter_share_distribution.callback(cog, interaction)

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs["embed"].title == "Megapool RPL per Borrowed ETH"
        assert kwargs["files"][0].filename == "voter_share_distribution.png"

    async def test_returns_no_data_message_when_empty(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        cog = Collateral(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.voter_share_distribution.callback(cog, interaction)

        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.description == "No data available."
