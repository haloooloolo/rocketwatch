from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.milestones.milestones import Milestones
from tests.lib.discord_harness import make_bot
from tests.lib.scripted_rocketpool import ScriptedRocketPool

_WEI = 10**18


def _seed_above_min(rp: ScriptedRocketPool) -> None:
    # All five milestone calls return values comfortably above their minimums.
    rp.set_call("rocketNodeStaking.getTotalStakedRPL", 500_000 * _WEI)  # → 500k
    rp.set_call("rocketTokenRETH.totalSupply", 50_000 * _WEI)  # → 50k
    rp.set_call("rocketTokenRPL.totalSwappedRPL", 17_100_000 * _WEI)  # → 95%
    rp.set_call("rocketNodeManager.getNodeCount", 5_000)
    rp.set_call("RockSolidVault.totalAssets", 10_000 * _WEI)  # → 10k


class TestMilestones:
    async def test_emits_event_per_crossed_milestone_then_idempotent(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        _seed_above_min(scripted_rp)
        cog = Milestones(make_bot(db=mongo_db))

        events = await cog._get_new_events()
        names = {e.event_name for e in events}
        # Every milestone crosses on first sighting.
        assert names == {
            "milestone_rpl_stake",
            "milestone_reth_supply",
            "milestone_rpl_swapped",
            "milestone_registered_nodes",
            "milestone_rocksolid_tvl",
        }

        # Goals are persisted; a second run at the same values emits nothing.
        assert await cog._get_new_events() == []

    async def test_value_below_min_is_skipped(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        _seed_above_min(scripted_rp)
        # Drop RPL staked below its 10k minimum.
        scripted_rp.set_call("rocketNodeStaking.getTotalStakedRPL", 5_000 * _WEI)

        cog = Milestones(make_bot(db=mongo_db))
        events = await cog._get_new_events()

        assert "milestone_rpl_stake" not in {e.event_name for e in events}
        # The skipped milestone is not recorded in the DB either.
        assert (
            await mongo_db.milestones.find_one({"_id": "milestone_rpl_stake"}) is None
        )
