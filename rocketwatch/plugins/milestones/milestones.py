import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, overload

from rocketwatch.bot import RocketWatch
from rocketwatch.utils import solidity
from rocketwatch.utils.embeds import Embed, format_value
from rocketwatch.utils.event import Event, EventPlugin
from rocketwatch.utils.rocketpool import rp

log = logging.getLogger("rocketwatch.milestones")


@dataclass(frozen=True, slots=True)
class Milestone:
    id: str
    description: str
    min: int
    step_size: int
    call: Callable[[], Awaitable[float | int]]


@overload
def contract_call[T](
    path: str, formatter: Callable[[Any], T]
) -> Callable[[], Awaitable[T]]: ...
@overload
def contract_call(path: str) -> Callable[[], Awaitable[Any]]: ...
def contract_call(
    path: str, formatter: Callable[[Any], Any] | None = None
) -> Callable[[], Awaitable[Any]]:
    async def call() -> Any:
        value = await rp.call(path)
        return formatter(value) if formatter else value

    return call


async def _get_percentage_rpl_swapped() -> float:
    value: float = solidity.to_float(await rp.call("rocketTokenRPL.totalSwappedRPL"))
    return round((value / 18_000_000) * 100, 2)


MILESTONES: list[Milestone] = [
    Milestone(
        id="milestone_rpl_stake",
        description="{} RPL has been staked by node operators!",
        min=10_000,
        step_size=100_000,
        call=contract_call("rocketNodeStaking.getTotalStakedRPL", solidity.to_float),
    ),
    Milestone(
        id="milestone_reth_supply",
        description="{} rETH has been issued!",
        min=1_000,
        step_size=5_000,
        call=contract_call("rocketTokenRETH.totalSupply", solidity.to_float),
    ),
    Milestone(
        id="milestone_rpl_swapped",
        description="{}% of all RPL has been exchanged for the new version!",
        min=90,
        step_size=1,
        call=_get_percentage_rpl_swapped,
    ),
    Milestone(
        id="milestone_registered_nodes",
        description="{} nodes have been registered!",
        min=50,
        step_size=100,
        call=contract_call("rocketNodeManager.getNodeCount"),
    ),
    Milestone(
        id="milestone_rocksolid_tvl",
        description="{} rETH deposited into the RockSolid vault!",
        min=0,
        step_size=5000,
        call=contract_call("RockSolidVault.totalAssets", solidity.to_float),
    ),
]


class Milestones(EventPlugin):
    def __init__(self, bot: RocketWatch):
        super().__init__(bot)
        self.collection = self.bot.db.milestones

    async def _get_new_events(self) -> list[Event]:
        log.info("Checking milestones")
        payload = []

        for milestone in MILESTONES:
            state = await self.collection.find_one({"_id": milestone.id})

            value = await milestone.call()
            log.debug(f"{milestone.id}:{value}")
            if value < milestone.min:
                continue

            step_size = milestone.step_size
            latest_goal = (value // step_size + 1) * step_size

            if state:
                previous_milestone = state["current_goal"]
            else:
                log.debug(
                    f"First time we have processed Milestones for milestone {milestone.id}. Adding it to the Database."
                )
                await self.collection.insert_one(
                    {"_id": milestone.id, "current_goal": latest_goal}
                )
                previous_milestone = milestone.min
            if previous_milestone < latest_goal:
                log.info(
                    f"Goal for milestone {milestone.id} has increased. Triggering Milestone!"
                )
                time = int(datetime.now().timestamp())
                embed = Embed(
                    title=":tada: Milestone Reached",
                    description=milestone.description.format(format_value(value)),
                )
                embed.add_field(
                    name="Timestamp",
                    value=f"<t:{time}:R> (<t:{time}:f>)",
                    inline=False,
                )
                payload.append(
                    Event(
                        embed=embed,
                        topic="milestones",
                        block_number=self._pending_block,
                        event_name=milestone.id,
                        unique_id=f"{milestone.id}:{latest_goal}",
                    )
                )
                # update the current goal in collection
                await self.collection.update_one(
                    {"_id": milestone.id}, {"$set": {"current_goal": latest_goal}}
                )

        log.debug("Finished checking milestones")
        return payload


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(Milestones(bot))
