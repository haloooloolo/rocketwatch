import logging

import aiohttp
from aiocache import cached
from bs4 import BeautifulSoup
from discord import Interaction
from discord.app_commands import Choice, command, describe
from discord.ext.commands import Cog

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.retry import retry

log = logging.getLogger("rocketwatch.rpips")


class RPIPs(Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    @command()
    @describe(name="RPIP name")
    async def rpip(self, interaction: Interaction, name: str) -> None:
        """Show information about a specific RPIP."""
        await interaction.response.defer()
        embed = Embed()
        embed.set_author(
            name="🔗 Data from rpips.rocketpool.net", url="https://rpips.rocketpool.net"
        )

        rpips_by_name: dict[str, RPIPs.RPIP] = {
            rpip.full_title: rpip for rpip in await self.get_all_rpips()
        }
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
            embed.add_field(
                name="Discussion Link", value=details["discussion"], inline=False
            )
        else:
            embed.description = "No matching RPIPs."

        await interaction.followup.send(embed=embed)

    class RPIP:
        __slots__ = ("number", "status", "title")

        def __init__(self, title: str, number: int, status: str):
            self.title = title
            self.number = number
            self.status = status

        def __str__(self) -> str:
            return self.full_title

        @cached(ttl=300, key_builder=lambda _, rpip: rpip.number)
        @retry(tries=3, delay=1)
        async def fetch_details(self) -> dict[str, str | list[str] | None]:
            async with (
                aiohttp.ClientSession() as session,
                session.get(self.url) as resp,
            ):
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")
            if not soup.main:
                return {}

            preamble = soup.main.find("table", {"class": "rpip-preamble"})
            if not preamble:
                return {}

            metadata: dict[str, str | list[str]] = {}
            for field in preamble.find_all("tr"):
                if field.th and field.td:
                    match field_name := field.th.text:
                        case "Discussion":
                            if field.td.a:
                                metadata[field_name] = field.td.a["href"]
                        case "Author":
                            metadata[field_name] = [
                                a.text for a in field.td.find_all("a")
                            ]
                        case _:
                            metadata[field_name] = field.td.text

            description_tag = soup.find("big", {"class": "rpip-description"})
            return {
                "type": metadata.get("Type"),
                "authors": metadata.get("Author"),
                "created": metadata.get("Created"),
                "discussion": metadata.get("Discussion"),
                "description": description_tag.text if description_tag else None,
            }

        @property
        def full_title(self) -> str:
            return f"RPIP-{self.number}: {self.title}"

        @property
        def url(self) -> str:
            return f"https://rpips.rocketpool.net/RPIPs/RPIP-{self.number}"

    @rpip.autocomplete("name")
    async def _get_rpip_names(
        self, interaction: Interaction, current: str
    ) -> list[Choice[str]]:
        choices = []
        for rpip in await self.get_all_rpips():
            if current.lower() in (name := rpip.full_title).lower():
                choices.append(Choice(name=name, value=name))
        return choices[:-26:-1]

    @staticmethod
    @cached(ttl=60)
    @retry(tries=3, delay=1)
    async def get_all_rpips() -> list["RPIPs.RPIP"]:
        async with (
            aiohttp.ClientSession() as session,
            session.get("https://rpips.rocketpool.net/all") as resp,
        ):
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        if not soup.table:
            return []

        rpips: list[RPIPs.RPIP] = []
        for row in soup.table.find_all("tr", recursive=False):
            title_td = row.find("td", {"class": "title"})
            num_td = row.find("td", {"class": "rpipnum"})
            status_td = row.find("td", {"class": "status"})
            if title_td and num_td and status_td:
                rpips.append(
                    RPIPs.RPIP(
                        title_td.text.strip(), int(num_td.text), status_td.text.strip()
                    )
                )

        return rpips


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(RPIPs(bot))
