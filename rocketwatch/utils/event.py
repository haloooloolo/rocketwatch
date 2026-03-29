from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from discord.ext import commands
from eth_typing import BlockNumber

if TYPE_CHECKING:
    from rocketwatch.rocketwatch import RocketWatch
from utils.config import cfg
from utils.embeds import Embed
from utils.image import Image
from utils.shared_w3 import w3


@dataclass(frozen=True, slots=True)
class Event:
    embed: Embed
    topic: str
    event_name: str
    unique_id: str
    block_number: BlockNumber
    transaction_index: int = 999
    event_index: int = 999
    image: Image | None = None
    thumbnail: Image | None = None

    def get_score(self) -> int:
        return (
            (10**9 * self.block_number)
            + (10**5 * self.transaction_index)
            + self.event_index
        )


class EventPlugin(commands.Cog):
    def __init__(
        self, bot: RocketWatch, rate_limit: timedelta = timedelta(seconds=5)
    ) -> None:
        self.bot = bot
        self.rate_limit = rate_limit
        self.lookback_distance: int = cfg.events.lookback_distance
        self.last_served_block = BlockNumber(cfg.events.genesis - 1)
        self._pending_block = self.last_served_block
        self._last_run = datetime.now() - rate_limit

    def start_tracking(self, block: BlockNumber) -> None:
        self.last_served_block = BlockNumber(block - 1)

    async def get_new_events(self) -> list[Event]:
        now = datetime.now()
        if (now - self._last_run) < self.rate_limit:
            return []

        self._last_run = now
        self._pending_block = await w3.eth.get_block_number()
        events = await self._get_new_events()
        self.last_served_block = self._pending_block
        return events

    @abstractmethod
    async def _get_new_events(self) -> list[Event]:
        pass

    async def get_past_events(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[Event]:
        return []
