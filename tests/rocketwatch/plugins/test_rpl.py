from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.rpl.rpl import RPL
from tests.lib.discord_harness import make_bot, make_interaction, run_command
from tests.lib.scripted_rocketpool import ScriptedRocketPool

pytestmark = pytest.mark.integration_db

ETH = 10**18


@pytest.fixture
def cog(
    mongo_db: AsyncDatabase[dict[str, Any]],
    scripted_rp: ScriptedRocketPool,
) -> RPL:
    return RPL(make_bot(db=mongo_db))


class TestStakedRpl:
    async def test_attaches_pie_chart_image(
        self,
        cog: RPL,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        await mongo_db.node_operators.insert_many([{"rpl": {"unstaking": 7_000.0}}])
        scripted_rp.set_call("rocketTokenRPL.totalSupply", 1_000_000 * ETH)
        scripted_rp.set_call("rocketNodeStaking.getTotalLegacyStakedRPL", 400_000 * ETH)
        scripted_rp.set_call(
            "rocketNodeStaking.getTotalMegapoolStakedRPL", 100_000 * ETH
        )

        interaction = make_interaction()
        embed = await run_command(cog, "staked_rpl", interaction)

        assert embed.title == "Staked RPL"
        assert embed.image.url == "attachment://graph.png"
        call_kwargs = interaction.followup.send.call_args.kwargs
        assert call_kwargs["file"].filename == "graph.png"


class TestWithdrawableRpl:
    async def test_filters_inactive_operators_and_attaches_chart(
        self,
        cog: RPL,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        await mongo_db.node_operators.insert_many(
            [
                {
                    "staking_minipool_count": 2,
                    "effective_node_share": 0.5,
                    "rpl": {"legacy_stake": 500.0},
                },
                {
                    "staking_minipool_count": 1,
                    "effective_node_share": 0.25,
                    "rpl": {"legacy_stake": 100.0},
                },
                # Filtered out by $match: no staking minipools.
                {
                    "staking_minipool_count": 0,
                    "effective_node_share": 0.0,
                    "rpl": {"legacy_stake": 999.0},
                },
            ]
        )
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", 5 * 10**16)
        scripted_rp.set_call(
            "rocketDAOProtocolSettingsNode.getMinimumLegacyRPLStake", 10 * 10**16
        )

        interaction = make_interaction()
        embed = await run_command(cog, "withdrawable_rpl", interaction)

        assert embed.title == "Available RPL Liquidity"
        assert embed.image.url == "attachment://graph.png"
        # This command uses files=[...] (plural) rather than file=.
        call_kwargs = interaction.followup.send.call_args.kwargs
        assert call_kwargs["files"][0].filename == "graph.png"
