import aiohttp
from discord import Interaction
from discord.app_commands import command
from discord.ext import commands

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.visibility import is_hidden


class AskDoofus(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    @command()
    async def ask_doofus(self, interaction: Interaction, question: str) -> None:
        """Ask Doofus a question about Rocket Pool governance."""
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        payload = {
            "mode": "general",
            "question": question,
            "stream": False,
        }

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                "https://rpgovsearch.com/api/ask",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp,
        ):
            resp.raise_for_status()
            data = await resp.json()

        answer = data.get("finalAnswer", "No answer received.")

        citations = data.get("citations", [])
        if citations:
            sources = "\n".join(
                f"[{c['tag']}]({c['url']}): {c['title']} > {c['heading']}"
                for c in citations
                if c.get("url") and c.get("tag")
            )
            if sources:
                answer += f"\n\n**Sources:**\n{sources}"

        e = Embed()
        e.title = "Ask Doofus"
        e.description = f"> {question}\n\n{answer}"
        await interaction.followup.send(embed=e)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(AskDoofus(bot))
