import logging

import cronitor
from discord import Activity, ActivityType
from discord.ext import commands, tasks

from rocketwatch import RocketWatch
from utils.cfg import cfg
from utils.rocketpool import rp

log = logging.getLogger("rich_activity")
log.setLevel(cfg["log_level"])

class RichActivity(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.monitor = cronitor.Monitor("update-activity", api_key=cfg["cronitor_secret"])
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    @tasks.loop(seconds=60)
    async def loop(self):
        self.monitor.ping()
        try:
            log.debug("Updating Discord activity")
            mp_count = rp.call("rocketMinipoolManager.getActiveMinipoolCount")
            await self.bot.change_presence(
                activity=Activity(
                    type=ActivityType.watching,
                    name=f"{mp_count:,} minipools!"
                )
            )
        except Exception as err:
            await self.bot.report_error(err)

    @loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(RichActivity(bot))
