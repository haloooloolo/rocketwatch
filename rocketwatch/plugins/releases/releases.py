import logging

import aiohttp
from discord import Interaction
from discord.app_commands import command
from discord.ext import commands

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.releases")


class Releases(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self._repo = "rocket-pool/smartnode"

    @command()
    async def latest_release(self, interaction: Interaction) -> None:
        """
        Show the latest release of Smart Node
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        async with aiohttp.ClientSession() as session:
            res = await session.get(
                f"https://api.github.com/repos/{self._repo}/releases"
            )
            releases = await res.json()

        latest_stable = None
        latest_prerelease = None
        tag_base_url = f"https://github.com/{self._repo}/releases/tag"
        for release in releases:
            tag = release["tag_name"]
            link = f"[{tag}]({tag_base_url}/{tag})"
            if release["prerelease"]:
                latest_prerelease = latest_prerelease or link
            else:
                latest_stable = link
                break

        e = Embed(title="Smart Node Releases")
        e.add_field(name="Latest Release", value=latest_stable or "N/A", inline=True)
        if latest_prerelease:
            e.add_field(name="Latest Pre-release", value=latest_prerelease, inline=True)
        await interaction.followup.send(embed=e)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(Releases(bot))
