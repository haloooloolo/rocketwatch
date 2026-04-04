from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

from discord.ext import commands

if TYPE_CHECKING:
    from rocketwatch import RocketWatch
from utils.embeds import Embed


class StatusPlugin(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    @abstractmethod
    async def get_status(self) -> Embed:
        pass
