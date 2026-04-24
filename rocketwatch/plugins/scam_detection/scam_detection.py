import asyncio
import logging

from discord import (
    AppCommandType,
    Guild,
    Interaction,
    Member,
    Message,
    MessageType,
    RawBulkMessageDeleteEvent,
    RawMessageDeleteEvent,
    RawThreadDeleteEvent,
    Thread,
    User,
)
from discord.app_commands import ContextMenu, command, guilds
from discord.ext.commands import Cog
from pymongo import ReturnDocument

from rocketwatch.bot import RocketWatch
from rocketwatch.plugins.scam_detection.checks import ScamChecks
from rocketwatch.plugins.scam_detection.common import (
    REPUTABLE_MESSAGE_THRESHOLD,
    ReportContext,
    is_reputable,
    member_from_message,
    resolve_report,
    update_report,
)
from rocketwatch.plugins.scam_detection.llm_check import LLMScamChecker
from rocketwatch.plugins.scam_detection.message_report import (
    WarningConfirmView,
    manual_message_report,
    on_message_delete,
    report_message,
)
from rocketwatch.plugins.scam_detection.thread_report import (
    check_thread_starter_deleted,
    on_thread_removed,
)
from rocketwatch.plugins.scam_detection.user_report import manual_user_report
from rocketwatch.plugins.scam_detection.views import ReportReviewView
from rocketwatch.utils.config import cfg
from rocketwatch.utils.sentinel import SentinelClient

log = logging.getLogger("rocketwatch.scam_detection")


class ScamDetection(Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self._ctx = ReportContext(
            bot=bot,
            sentinel=SentinelClient(),
        )
        self._checks = ScamChecks()
        self._llm_check = LLMScamChecker()
        self._thread_creation_messages: dict[int, int] = {}
        self.message_report_menu = ContextMenu(
            name="Report Message",
            callback=self._manual_message_report,
            guild_ids=[cfg.rocketpool.support.server_id],
        )
        self.bot.tree.add_command(self.message_report_menu)
        self.user_report_menu = ContextMenu(
            name="Report User",
            callback=self._manual_user_report,
            type=AppCommandType.user,
            guild_ids=[cfg.rocketpool.support.server_id],
        )
        self.bot.tree.add_command(self.user_report_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(
            self.message_report_menu.name, type=self.message_report_menu.type
        )
        self.bot.tree.remove_command(
            self.user_report_menu.name, type=self.user_report_menu.type
        )

    # --- Listeners ---

    @Cog.listener()
    async def on_message(self, message: Message) -> None:
        log.debug(
            f"Message(id={message.id}, author={message.author}, channel={message.channel},"
            f' content="{message.content}", embeds={message.embeds})'
        )

        if message.guild is None:
            return

        if message.guild.id != cfg.rocketpool.support.server_id:
            log.warning(f"Ignoring message in {message.guild.id})")
            return

        if message.author.bot:
            log.warning("Ignoring message sent by bot")
            return

        user_msg_count = await self._increment_message_count(message.author)

        if (message.type == MessageType.thread_created) and message.reference:
            thread_id = message.reference.channel_id
            self._thread_creation_messages.pop(thread_id, None)
            self._thread_creation_messages[message.id] = thread_id
            return

        if (not message.content) and (not message.embeds):
            log.debug("Ignoring message with empty content")
            return

        member = await member_from_message(self.bot, message)
        if member and is_reputable(member):
            log.info(f"Ignoring message sent by trusted user ({message.author})")
            return

        if reason := self._checks.run_all(message):
            await report_message(self._ctx, message, reason)
            return

        if user_msg_count >= REPUTABLE_MESSAGE_THRESHOLD:
            log.debug(
                f"Ignoring message because user has {user_msg_count} previous messages"
            )
            return

        if (
            message.mentions
            and (not message.reference)
            and isinstance(message.channel, Thread)
            and (message.channel.owner_id == message.author.id)
        ):
            await report_message(self._ctx, message, "Pinged user in new thread")
            return

        try:
            result = await self._llm_check.check(message, user_msg_count=user_msg_count)
            if result:
                await report_message(self._ctx, message, f"{result} (AI Detection)")
        except Exception as e:
            await self.bot.report_error(e)

    @Cog.listener()
    async def on_message_edit(self, before: Message, after: Message) -> None:
        await self.on_message(after)

    @Cog.listener()
    async def on_raw_message_delete(self, event: RawMessageDeleteEvent) -> None:
        await check_thread_starter_deleted(
            self._ctx, event.message_id, self._thread_creation_messages
        )
        await on_message_delete(self._ctx, event.message_id)

    @Cog.listener()
    async def on_raw_bulk_message_delete(
        self, event: RawBulkMessageDeleteEvent
    ) -> None:
        await asyncio.gather(
            *[self._on_single_message_delete(msg_id) for msg_id in event.message_ids]
        )

    async def _on_single_message_delete(self, message_id: int) -> None:
        await check_thread_starter_deleted(
            self._ctx, message_id, self._thread_creation_messages
        )
        await on_message_delete(self._ctx, message_id)

    @Cog.listener()
    async def on_thread_create(self, thread: Thread) -> None:
        if thread.guild.id == cfg.rocketpool.support.server_id:
            self._thread_creation_messages[thread.id] = thread.id

    @Cog.listener()
    async def on_thread_update(self, before: Thread, after: Thread) -> None:
        if before.locked or (not after.locked):
            return
        await on_thread_removed(self._ctx, after.id, "Thread has been locked.")

    @Cog.listener()
    async def on_raw_thread_delete(self, event: RawThreadDeleteEvent) -> None:
        await on_thread_removed(self._ctx, event.thread_id, "Thread has been deleted.")

    @Cog.listener()
    async def on_member_update(self, before: Member, after: Member) -> None:
        if before.is_timed_out() == after.is_timed_out():
            return

        db_filter = {"guild_id": after.guild.id, "user_id": after.id}
        reports = await self.bot.db.scam_reports.find(db_filter).to_list()
        if not reports:
            return

        if before.is_timed_out() or (not after.is_timed_out()):
            # can't detect timeouts being lifted here
            # indistinguishable from a timeout expiring
            return

        await asyncio.gather(
            *[
                update_report(
                    self._ctx, report["report_id"], "User has been timed out."
                )
                for report in reports
            ]
        )

    @Cog.listener()
    async def on_member_ban(self, guild: Guild, user: User) -> None:
        db_filter = {"guild_id": guild.id, "user_id": user.id}
        if reports := await self.bot.db.scam_reports.find(db_filter).to_list():
            msg = "User has been banned."
            await asyncio.gather(
                *[
                    resolve_report(self._ctx, report["report_id"], msg)
                    for report in reports
                ]
            )

    # --- Commands ---

    @command()
    @guilds(cfg.rocketpool.support.server_id)
    async def report_user(
        self,
        interaction: Interaction[RocketWatch],
        user: Member,
        reason: str = "",
    ) -> None:
        """Generate a suspicious user report and send it to the report channel"""
        await manual_user_report(self._ctx, interaction, user, reason)

    async def _manual_message_report(
        self, interaction: Interaction[RocketWatch], message: Message
    ) -> None:
        await manual_message_report(self._ctx, interaction, message)

    async def _manual_user_report(
        self, interaction: Interaction[RocketWatch], user: Member
    ) -> None:
        await manual_user_report(self._ctx, interaction, user)

    # --- Helpers ---

    async def _increment_message_count(self, user: User | Member) -> int:
        result = await self.bot.db.message_counts.find_one_and_update(
            {"_id": user.id},
            {"$inc": {"count": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(result["count"]) if result else 0


async def setup(bot: RocketWatch) -> None:
    bot.add_view(ReportReviewView())
    bot.add_view(WarningConfirmView())
    await bot.add_cog(ScamDetection(bot))
