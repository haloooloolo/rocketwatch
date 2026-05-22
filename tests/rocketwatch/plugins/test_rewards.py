from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from rocketwatch.plugins.rewards.rewards import Rewards
from tests.lib.discord_harness import make_bot, make_interaction
from tests.lib.scripted_rocketpool import ScriptedRocketPool, addr

ETH = 10**18
NODE = addr("0x" + "ab" * 20)


def _patches_response(node: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "time": 1_700_000_000,
        "interval": 30,
        "startTime": 1_699_000_000,
        "totalNodeWeight": 1000 * ETH,
        node: {"collateralRpl": 5 * ETH, "smoothingPoolEth": 1 * ETH},
    }
    base.update(overrides)
    return base


@pytest.fixture
def _stub_externals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # resolve_ens echoes the address through; ts_to_block is deterministic.
    async def fake_resolve(_interaction: Any, node: str) -> tuple[str, str]:
        return node, node

    async def fake_ts_to_block(_ts: int) -> int:
        return 20_000_000

    monkeypatch.setattr("rocketwatch.plugins.rewards.rewards.resolve_ens", fake_resolve)
    monkeypatch.setattr(
        "rocketwatch.plugins.rewards.rewards.ts_to_block", fake_ts_to_block
    )
    yield


class TestCreateEmbed:
    def test_renders_interval_and_timestamps(self) -> None:
        rewards = Rewards.RewardEstimate(
            address=NODE,
            interval=30,
            start_time=1_699_000_000,
            data_time=1_700_000_000,
            data_block=20_000_000,
            end_time=1_701_000_000,
            rpl_rewards=5.0,
            eth_rewards=1.0,
            system_weight=1000.0,
        )
        embed = Rewards.create_embed("My Title", rewards)
        assert embed.title == "My Title"
        assert embed.description is not None
        assert "interval 30" in embed.description
        assert "<t:1700000000" in embed.description


class TestGetEstimatedRewards:
    async def test_unregistered_node_returns_none(
        self, scripted_rp: ScriptedRocketPool, _stub_externals: None
    ) -> None:
        scripted_rp.set_call("rocketNodeManager.getNodeExists", False)
        cog = Rewards(make_bot())
        interaction = make_interaction()
        result = await cog.get_estimated_rewards(interaction, NODE)
        assert result is None
        interaction.followup.send.assert_awaited_once()
        assert "not a registered node" in interaction.followup.send.call_args.args[0]

    async def test_api_error_reports_and_returns_none(
        self,
        scripted_rp: ScriptedRocketPool,
        _stub_externals: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scripted_rp.set_call("rocketNodeManager.getNodeExists", True)
        cog = Rewards(make_bot())
        monkeypatch.setattr(
            cog, "_make_request", AsyncMock(side_effect=RuntimeError("patches down"))
        )
        interaction = make_interaction()
        result = await cog.get_estimated_rewards(interaction, NODE)
        assert result is None
        cog.bot.report_error.assert_awaited_once()
        assert "Blame Patches" in interaction.followup.send.call_args.args[0]

    async def test_success_builds_estimate(
        self,
        scripted_rp: ScriptedRocketPool,
        _stub_externals: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scripted_rp.set_call("rocketNodeManager.getNodeExists", True)
        scripted_rp.set_call(
            "rocketDAOProtocolSettingsRewards.getRewardsClaimIntervalTime",
            28 * 24 * 3600,
        )
        cog = Rewards(make_bot())
        monkeypatch.setattr(
            cog, "_make_request", AsyncMock(return_value=_patches_response(NODE))
        )
        interaction = make_interaction()
        result = await cog.get_estimated_rewards(interaction, NODE)
        assert result is not None
        assert result.interval == 30
        assert result.rpl_rewards == pytest.approx(5.0)
        assert result.eth_rewards == pytest.approx(1.0)
        assert result.end_time == 1_699_000_000 + 28 * 24 * 3600


class TestUpcomingRewards:
    async def test_ens_failure_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def failing_resolve(_i: Any, _n: str) -> tuple[None, None]:
            return None, None

        monkeypatch.setattr(
            "rocketwatch.plugins.rewards.rewards.resolve_ens", failing_resolve
        )
        cog = Rewards(make_bot())
        interaction = make_interaction()
        await cog.upcoming_rewards.callback(cog, interaction, node_address="bad.eth")
        interaction.followup.send.assert_not_awaited()

    async def test_non_extrapolated_reports_raw_values(
        self,
        scripted_rp: ScriptedRocketPool,
        _stub_externals: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scripted_rp.set_call("rocketNodeManager.getNodeExists", True)
        scripted_rp.set_call(
            "rocketDAOProtocolSettingsRewards.getRewardsClaimIntervalTime",
            28 * 24 * 3600,
        )
        cog = Rewards(make_bot())
        monkeypatch.setattr(
            cog, "_make_request", AsyncMock(return_value=_patches_response(NODE))
        )
        interaction = make_interaction()
        await cog.upcoming_rewards.callback(
            cog, interaction, node_address=NODE, extrapolate=False
        )

        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.title is not None
        assert "Estimated Ongoing" in embed.title
        fields = {f.name: f.value for f in embed.fields}
        assert "5.000 RPL" in fields["RPL Staking:"]
        assert "1.000 ETH" in fields["Smoothing Pool:"]

    async def test_extrapolated_scales_by_projection(
        self,
        scripted_rp: ScriptedRocketPool,
        _stub_externals: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scripted_rp.set_call("rocketNodeManager.getNodeExists", True)
        scripted_rp.set_call(
            "rocketDAOProtocolSettingsRewards.getRewardsClaimIntervalTime",
            28 * 24 * 3600,
        )
        # Registered before the period started, so reward_start_time == startTime.
        scripted_rp.set_call("rocketNodeManager.getNodeRegistrationTime", 1_600_000_000)
        cog = Rewards(make_bot())
        monkeypatch.setattr(
            cog, "_make_request", AsyncMock(return_value=_patches_response(NODE))
        )
        interaction = make_interaction()
        await cog.upcoming_rewards.callback(
            cog, interaction, node_address=NODE, extrapolate=True
        )

        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.title is not None
        assert "Projected" in embed.title
        # proj_factor = (end - start) / (data_time - start)
        #            = 2419200 / 1000000 = 2.4192 → 5 RPL * 2.4192 ≈ 12.096
        fields = {f.name: f.value for f in embed.fields}
        assert "12.096 RPL" in fields["RPL Staking:"]


class TestSimulateRewards:
    async def _setup_cog(
        self,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
        *,
        actual_borrowed: float,
        actual_stake: float,
    ) -> Rewards:
        scripted_rp.set_call("rocketNodeManager.getNodeExists", True)
        scripted_rp.set_call(
            "rocketDAOProtocolSettingsRewards.getRewardsClaimIntervalTime",
            28 * 24 * 3600,
        )
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", 5 * 10**16)
        scripted_rp.set_call(
            "rocketNodeStaking.getNodeETHBorrowed", int(actual_borrowed * ETH)
        )
        scripted_rp.set_call(
            "rocketNodeStaking.getNodeStakedRPL", int(actual_stake * ETH)
        )
        scripted_rp.set_call(
            "rocketTokenRPL.getInflationIntervalRate", 1000000000133680617
        )
        scripted_rp.set_call("rocketTokenRPL.getInflationIntervalTime", 24 * 3600)
        scripted_rp.set_call("rocketTokenRPL.totalSupply", 18_000_000 * ETH)
        cog = Rewards(make_bot())
        monkeypatch.setattr(
            cog, "_make_request", AsyncMock(return_value=_patches_response(NODE))
        )
        return cog

    async def test_empty_node_reports_message(
        self,
        scripted_rp: ScriptedRocketPool,
        _stub_externals: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No actual borrowed eth and no simulated minipools → "Empty node".
        cog = await self._setup_cog(
            scripted_rp, monkeypatch, actual_borrowed=0, actual_stake=0
        )
        interaction = make_interaction()
        await cog.simulate_rewards.callback(cog, interaction, node_address=NODE)
        msg = interaction.followup.send.call_args.args[0]
        assert "Empty node" in msg

    async def test_renders_chart_for_active_node(
        self,
        scripted_rp: ScriptedRocketPool,
        _stub_externals: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cog = await self._setup_cog(
            scripted_rp, monkeypatch, actual_borrowed=24.0, actual_stake=500.0
        )
        interaction = make_interaction()
        await cog.simulate_rewards.callback(
            cog, interaction, node_address=NODE, rpl_stake=1000, num_leb8=2
        )
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs["embed"].title is not None
        assert "Simulated RPL Rewards" in kwargs["embed"].title
        assert kwargs["files"][0].filename == "rewards.png"
