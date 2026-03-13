import asyncio
import random
import random as pyrandom

from discord import Interaction
from discord.app_commands import command
from discord.ext import commands

from rocketwatch import RocketWatch
from utils.embeds import Embed
from utils.visibility import is_hidden


class EightBall(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    @command(name="8ball")
    async def eight_ball(self, interaction: Interaction, question: str):
        e = Embed(title="🎱 Magic 8 Ball")
        if not question.endswith("?"):
            e.description = (
                "You must ask a yes or no question to the magic 8 ball"
                " (hint: add a `?` at the end of your question)"
            )
            await interaction.response.send_message(embed=e, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        await asyncio.sleep(random.randint(2, 5))
        res = pyrandom.choice(
            [
                "As I see it, yes",
                "It is certain",
                "It is decidedly so",
                "Most likely",
                "Outlook good",
                "Signs point to yes",
                "Without a doubt",
                "Yes",
                "Yes - definitely",
                "You may rely on it",
                "Don't count on it",
                "My reply is no",
                "My sources say no",
                "Outlook not so good",
                "Very doubtful",
                "Chances aren't good",
                "Unlikely",
                "Not likely",
                "No",
                "Absolutely not",
            ]
        )
        e.description = f'> "{question}"\n - `{interaction.user.display_name}`\n\nThe Magic 8 Ball says: `{res}`'
        await interaction.followup.send(embed=e)


async def setup(bot):
    await bot.add_cog(EightBall(bot))
