from discord import Interaction
from discord.ext import commands
from discord.app_commands import command
from rocketwatch import RocketWatch

from datetime import datetime, timedelta


class ChickenSoup(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.duration = timedelta(minutes=5)
        self.dispense_end = {}
        
    @command()
    async def chicken_soup(self, interaction: Interaction):
        self.dispense_end[interaction.channel_id] = datetime.now() + self.duration
        await interaction.response.send_message(
            "https://tenor.com/view/muppets-muppet-show-swedish-chef-chicken-pot-gif-9362214582988742217"
        )

    @commands.Cog.listener()
    async def on_message(self, message) -> None:
        if message.author == self.bot.user:
            return

        if message.channel.id not in self.dispense_end:
            return

        if datetime.now() > self.dispense_end[message.channel.id]:
            del self.dispense_end[message.channel.id]
            return

        await message.add_reaction("🐔")
        await message.add_reaction("🍲")


async def setup(bot):
    await bot.add_cog(ChickenSoup(bot))
