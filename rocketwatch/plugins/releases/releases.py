import logging

import aiohttp
from discord import Interaction
from discord.app_commands import command
from discord.ext import commands

from rocketwatch import RocketWatch
from utils.embeds import Embed
from utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.releases")


class Releases(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.tag_url = "https://github.com/rocket-pool/smartnode-install/releases/tag/"

    @command()
    async def latest_release(self, interaction: Interaction) -> None:
        """
        Get the latest release of Smart Node.
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        async with aiohttp.ClientSession() as session:
            res = await session.get(
                "https://api.github.com/repos/rocket-pool/smartnode-install/tags"
            )
            res = await res.json()
        latest_release = None
        for tag in res:
            if tag["name"].split(".")[-1].isnumeric():
                latest_release = f"[{tag['name']}]({self.tag_url + tag['name']})"
                break

        e = Embed()
        e.add_field(
            name="Latest Smart Node Release", value=latest_release, inline=False
        )
        await interaction.followup.send(embed=e)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(Releases(bot))
