import logging
from datetime import UTC, datetime, timedelta

from bson import ObjectId
from discord import Interaction, Member, Message, User, errors
from discord.abc import Messageable
from discord.app_commands import command, guilds
from discord.ext import commands
from discord.ext.commands import is_owner

from rocketwatch import RocketWatch
from utils.config import cfg
from utils.embeds import Embed

log = logging.getLogger("rocketwatch.scam_warning")


class ScamWarning(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.channel_ids = set(cfg.rocketpool.dm_warning.channels)
        self.inactivity_cooldown = timedelta(days=90)
        self.failure_cooldown = timedelta(days=1)

    async def _build_warning_embed(self) -> Embed:
        support_channel = await self.bot.get_or_fetch_channel(
            cfg.rocketpool.support.channel_id
        )
        resource_channel = await self.bot.get_or_fetch_channel(
            cfg.discord.channels["resources"]
        )
        assert isinstance(support_channel, Messageable)
        assert isinstance(resource_channel, Messageable)

        since = datetime.now(UTC) - timedelta(days=30)
        scam_count = await self.bot.db.scam_reports.count_documents(
            {"_id": {"$gt": ObjectId.from_datetime(since)}}
        )

        title = "**Stay Safe on Rocket Pool Discord**"
        description = "Scammers actively target this server."
        if scam_count > 0:
            description += (
                f" Rocket Watch has detected **{scam_count} scam attempts**"
                f" in the past month alone - many more go unnoticed."
            )
        description += (
            f"\n\n"
            f"🔑 **Protect your keys**\n"
            f"Never share your private keys or seed phrase."
            f" Under no circumstance are they needed to resolve your issue.\n"
            f"\n"
            f"🚫 **Ignore unsolicited DMs**\n"
            f"We do **not** use a ticket system."
            f" Use the public {support_channel.mention} channel for help."
            f" Be aware that scammers may try to impersonate reputable users.\n"
            f"\n"
            f"🔗 **Verify links**\n"
            f"Find official links and contract addresses"
            f" in {resource_channel.mention}. Always double-check URLs.\n"
            f"\n"
            f"> **Tip:** Right-click the server icon → *Privacy Settings* →"
            f" disable *Direct Messages* to block unsolicited messages.\n"
            f"\n"
            f"*This message may be sent again as a reminder after periods of inactivity.*"
        )

        return Embed(title=title, description=description)

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def preview_scam_warning(self, interaction: Interaction) -> None:
        """Preview the scam warning template"""
        embed = await self._build_warning_embed()
        await interaction.response.send_message(embed=embed)

    async def send_warning(self, user: User | Member) -> None:
        embed = await self._build_warning_embed()
        await user.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        # message not in relevant channel
        if message.channel.id not in self.channel_ids:
            return

        # don't let the bot try to DM itself
        if message.author == self.bot.user:
            return

        if (
            isinstance(message.author, Member)
            and message.author.guild_permissions.moderate_members
        ):
            log.info(f"{message.author} is a moderator, skipping warning.")
            return

        msg_time = message.created_at.replace(tzinfo=None)
        db_entry = (
            await self.bot.db.scam_warning.find_one({"_id": message.author.id})
        ) or {}

        cooldown_end = datetime.fromtimestamp(0)
        if last_failure_time := db_entry.get("last_failure"):
            cooldown_end = last_failure_time + self.failure_cooldown
        elif last_msg_time := db_entry.get("last_message"):
            cooldown_end = last_msg_time + self.inactivity_cooldown

        last_success_time = db_entry.get("last_success")

        # only send if message is not within cooldown window
        if msg_time > cooldown_end:
            try:
                await self.send_warning(message.author)
                last_failure_time = None
                last_success_time = msg_time
            except (errors.Forbidden, errors.HTTPException):
                log.info(f"Unable to DM {message.author}, skipping warning.")
                last_failure_time = msg_time

        await self.bot.db.scam_warning.replace_one(
            {"_id": message.author.id},
            {
                "_id": message.author.id,
                "last_message": msg_time,
                "last_success": last_success_time,
                "last_failure": last_failure_time,
            },
            upsert=True,
        )


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(ScamWarning(bot))
