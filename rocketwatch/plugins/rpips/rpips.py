import logging

import aiohttp
from aiocache import cached
from bs4 import BeautifulSoup

from discord import Interaction
from discord.ext.commands import Cog
from discord.app_commands import Choice, command, describe

from rocketwatch import RocketWatch
from utils.cfg import cfg
from utils.embeds import Embed
from utils.retry import retry_async

log = logging.getLogger("rpips")
log.setLevel(cfg["log_level"])


class RPIPs(Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    @command()
    @describe(name="RPIP name")
    async def rpip(self, interaction: Interaction, name: str):
        """Show information about a specific RPIP."""
        await interaction.response.defer()
        embed = Embed()
        embed.set_author(name="🔗 Data from rpips.rocketpool.net", url="https://rpips.rocketpool.net")

        rpips_by_name: dict[str, RPIPs.RPIP] = {rpip.full_title: rpip for rpip in await self.get_all_rpips()}
        if rpip := rpips_by_name.get(name):
            details = await rpip.fetch_details()
            embed.title = name
            embed.url = rpip.url
            embed.description = details["description"]

            authors = details["authors"]
            if len(authors) == 1:
                embed.add_field(name="Author", value=authors[0])
            else:
                embed.add_field(name="Authors", value=", ".join(authors))

            embed.add_field(name="Status", value=rpip.status)
            embed.add_field(name="Created", value=details["created"])
            embed.add_field(name="Discussion Link", value=details["discussion"], inline=False)
        else:
            embed.description = "No matching RPIPs."

        await interaction.followup.send(embed=embed)

    class RPIP:
        __slots__ = ("title", "number", "status")

        def __init__(self, title: str, number: int, status: str):
            self.title = title
            self.number = number
            self.status = status

        def __str__(self) -> str:
            return self.full_title

        @cached(ttl=300, key_builder=lambda _, rpip: rpip.number)
        @retry_async(tries=3, delay=1)
        async def fetch_details(self) -> dict:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.url) as resp:
                    html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")
            metadata = {}

            for field in soup.main.find("table", {"class": "rpip-preamble"}).find_all("tr"):
                match field_name := field.th.text:
                    case "Discussion":
                        metadata[field_name] = field.td.a["href"]
                    case "Author":
                        metadata[field_name] = [a.text for a in field.td.find_all("a")]
                    case _:
                        metadata[field_name] = field.td.text

            return {
                "type": metadata.get("Type"),
                "authors": metadata.get("Author"),
                "created": metadata.get("Created"),
                "discussion": metadata.get("Discussion"),
                "description": soup.find("big", {"class": "rpip-description"}).text
            }

        @property
        def full_title(self) -> str:
            return f"RPIP-{self.number}: {self.title}"

        @property
        def url(self) -> str:
            return f"https://rpips.rocketpool.net/RPIPs/RPIP-{self.number}"

    @rpip.autocomplete("name")
    async def _get_rpip_names(self, interaction: Interaction, current: str) -> list[Choice[str]]:
        choices = []
        for rpip in await self.get_all_rpips():
            if current.lower() in (name := rpip.full_title).lower():
                choices.append(Choice(name=name, value=name))
        return choices[:-26:-1]

    @staticmethod
    @cached(ttl=60)
    @retry_async(tries=3, delay=1)
    async def get_all_rpips() -> list['RPIPs.RPIP']:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://rpips.rocketpool.net/all") as resp:
                html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        rpips: list['RPIPs.RPIP'] = []

        for row in soup.table.find_all("tr", recursive=False):
            title = row.find("td", {"class": "title"}).text.strip()
            rpip_num = int(row.find("td", {"class": "rpipnum"}).text)
            status = row.find("td", {"class": "status"}).text.strip()
            rpips.append(RPIPs.RPIP(title, rpip_num, status))

        return rpips


async def setup(bot):
    await bot.add_cog(RPIPs(bot))
