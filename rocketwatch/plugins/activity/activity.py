import logging

from cronitor import Monitor
from discord import Activity, ActivityType
from discord.ext import commands, tasks

from rocketwatch import RocketWatch
from utils.config import cfg

log = logging.getLogger("rocketwatch.rich_activity")


class RichActivity(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.monitor = Monitor("update-activity", api_key=cfg.other.secrets.cronitor)
        self.task.start()

    async def cog_unload(self) -> None:
        self.task.cancel()

    @tasks.loop(minutes=5)
    async def task(self) -> None:
        self.monitor.ping()
        log.debug("Updating Discord activity")

        minipool_count = await self.bot.db.minipools.count_documents(
            {"beacon.status": "active_ongoing"}
        )
        megapool_count = await self.bot.db.megapool_validators.count_documents(
            {"beacon.status": "active_ongoing"}
        )
        validator_count = minipool_count + megapool_count
        await self.bot.change_presence(
            activity=Activity(
                type=ActivityType.watching,
                name=f"{validator_count:,} active validators",
            )
        )

    @task.before_loop
    async def before_loop(self) -> None:
        await self.bot.wait_until_ready()

    @task.error
    async def on_error(self, err: BaseException) -> None:
        assert isinstance(err, Exception)
        await self.bot.report_error(err)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(RichActivity(bot))
