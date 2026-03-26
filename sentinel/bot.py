import discord
from discord.ext.commands import Bot


class SentinelBot(Bot):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix=(), intents=intents)

    async def on_ready(self) -> None:
        print(f"Sentinel connected as {self.user}")
