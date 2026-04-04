import asyncio
import hashlib
from typing import NamedTuple

from discord import Color, Interaction
from discord.app_commands import command
from discord.ext import commands

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.visibility import is_hidden


class House(NamedTuple):
    name: str
    color: Color
    traits: str


HOUSES = [
    House("Gryffindor", Color.from_rgb(174, 0, 1), "Courage, Bravery, Determination"),
    House(
        "Slytherin", Color.from_rgb(26, 71, 42), "Ambition, Cunning, Resourcefulness"
    ),
    House("Ravenclaw", Color.from_rgb(34, 47, 91), "Wit, Wisdom, Creativity"),
    House("Hufflepuff", Color.from_rgb(236, 185, 57), "Loyalty, Patience, Hard Work"),
]

CLAPPING_GIF = "https://c.tenor.com/zVsoLH3HoQcAAAAd/tenor.gif"

PHRASES: dict[str, list[str]] = {
    "Gryffindor": [
        "Plenty of courage, I see...",
        "A heart full of bravery, no doubt about it...",
        "Not afraid of a challenge, are you?",
        "I see a daring spirit within you...",
    ],
    "Slytherin": [
        "You could be great, you know...",
        "Ah, I see ambition burning bright...",
        "Cunning and resourceful... yes, I see it clearly...",
        "A thirst to prove yourself, how interesting...",
    ],
    "Ravenclaw": [
        "Not a bad mind, either...",
        "A sharp wit and a curious soul...",
        "Such a thirst for knowledge...",
        "Wisdom beyond your years, I see...",
    ],
    "Hufflepuff": [
        "There's talent, oh my goodness, yes...",
        "Loyal to the core, I can tell...",
        "A dedication like no other...",
        "Patient and true... yes, I know where you belong...",
    ],
}


class SortingHat(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    @command(name="sorting_hat")
    async def sorting_hat(self, interaction: Interaction) -> None:
        """Find out which Hogwarts house you belong to!"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        await asyncio.sleep(3)

        digest = hashlib.sha256(str(interaction.user.id).encode()).digest()
        house = HOUSES[digest[0] % 4]

        phrases = PHRASES[house.name]
        embed = Embed(
            title="The Sorting Hat has decided!",
            color=house.color,
            description=(
                f"*{phrases[digest[1] % len(phrases)]}*\n# {house.name.upper()}!"
            ),
        )
        embed.add_field(name="Known for", value=house.traits, inline=False)
        embed.set_image(url=CLAPPING_GIF)

        await interaction.followup.send(embed=embed)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(SortingHat(bot))
