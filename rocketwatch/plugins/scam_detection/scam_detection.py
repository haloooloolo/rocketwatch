import asyncio
import contextlib
import json
import logging
from datetime import timedelta
from typing import Any

import humanize
from discord import (
    AppCommandType,
    DeletedReferencedMessage,
    File,
    Interaction,
    Member,
    Message,
    MessageType,
    RawBulkMessageDeleteEvent,
    RawMessageDeleteEvent,
    RawThreadDeleteEvent,
    Thread,
    User,
    errors,
)
from discord import (
    utils as discord_utils,
)
from discord.abc import Messageable
from discord.app_commands import ContextMenu, command, guilds
from discord.ext.commands import Cog
from pymongo import ReturnDocument

from rocketwatch.bot import RocketWatch
from rocketwatch.plugins.scam_detection.checks import ScamChecks
from rocketwatch.plugins.scam_detection.llm_check import LLMScamChecker
from rocketwatch.plugins.scam_detection.utils import (
    ReportColor,
    ReportReviewView,
    WarningConfirmView,
    is_reputable,
)
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.file import TextFile
from rocketwatch.utils.sentinel import SentinelClient

log = logging.getLogger("rocketwatch.scam_detection")

REPUTABLE_MESSAGE_THRESHOLD = 50
DEFAULT_USER_TIMEOUT = timedelta(hours=24)
MESSAGE_ALERT_DELETE_AFTER = timedelta(minutes=2)
THREAD_ALERT_DELETE_AFTER = timedelta(minutes=60)


class ScamDetection(Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self._message_report_lock = asyncio.Lock()
        self._thread_report_lock = asyncio.Lock()
        self._user_report_lock = asyncio.Lock()
        self._sentinel = SentinelClient()
        self._checks = ScamChecks()
        self._llm_check = LLMScamChecker()
        self._thread_creation_messages: dict[int, int] = {}
        self.message_report_menu = ContextMenu(
            name="Report Message",
            callback=self.manual_message_report,
            guild_ids=[cfg.rocketpool.support.server_id],
        )
        self.bot.tree.add_command(self.message_report_menu)
        self.user_report_menu = ContextMenu(
            name="Report User",
            callback=self.manual_user_report,
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

        # Thread-created system messages (from existing messages) have a different
        # ID than the thread. Map the system message ID to the thread ID so we can
        # detect deletion of the starter message.
        if message.type == MessageType.thread_created and message.reference:
            thread_id = message.reference.channel_id
            self._thread_creation_messages.pop(thread_id, None)
            self._thread_creation_messages[message.id] = thread_id
            return

        if isinstance(message.author, Member) and is_reputable(message.author):
            log.info(f"Ignoring message sent by trusted user ({message.author})")
            return

        if reason := self._checks.run_all(message):
            await self.report_message(message, reason)
            return

        if (not message.content) and (not message.embeds):
            log.debug("Ignoring message with empty content")
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
            await self.report_message(message, "Pinged user in new thread")
            return

        try:
            result = await self._llm_check.check(message, user_msg_count=user_msg_count)
            if result:
                await self.report_message(message, f"{result} (AI Detection)")
        except Exception as e:
            await self.bot.report_error(e)

    @Cog.listener()
    async def on_message_edit(self, before: Message, after: Message) -> None:
        await self.on_message(after)

    @Cog.listener()
    async def on_raw_message_delete(self, event: RawMessageDeleteEvent) -> None:
        await self._on_message_delete(event.message_id)

    @Cog.listener()
    async def on_raw_bulk_message_delete(
        self, event: RawBulkMessageDeleteEvent
    ) -> None:
        await asyncio.gather(
            *[self._on_message_delete(msg_id) for msg_id in event.message_ids]
        )

    @Cog.listener()
    async def on_thread_create(self, thread: Thread) -> None:
        if thread.guild.id == cfg.rocketpool.support.server_id:
            # For threads created along with their first message, the
            # announcement system message will share an ID with the thread
            # If this is a thread from an existing message, on_message will update
            # the mapping with the actual system message ID.
            self._thread_creation_messages[thread.id] = thread.id

    @Cog.listener()
    async def on_thread_update(self, before: Thread, after: Thread) -> None:
        if not before.locked and after.locked:
            db_filter = {"channel_id": after.id, "removed": False}
            async with self._thread_report_lock:
                if report := await self.bot.db.scam_reports.find_one(db_filter):
                    await self._update_report(
                        report["report_id"], "Thread has been locked."
                    )
                    await self.bot.db.scam_reports.update_one(
                        db_filter, {"$set": {"removed": True}}
                    )

    @Cog.listener()
    async def on_raw_thread_delete(self, event: RawThreadDeleteEvent) -> None:
        db_filter = {"channel_id": event.thread_id, "removed": False}
        async with self._thread_report_lock:
            if report := await self.bot.db.scam_reports.find_one(db_filter):
                await self._update_report(
                    report["report_id"], "Thread has been deleted."
                )
                await self.bot.db.scam_reports.update_one(
                    db_filter, {"$set": {"warning_id": None, "removed": True}}
                )

    # --- Commands ---

    @command()
    @guilds(cfg.rocketpool.support.server_id)
    async def report_user(self, interaction: Interaction, user: Member) -> None:
        """Generate a suspicious user report and send it to the report channel"""
        await self.manual_user_report(interaction, user)

    async def manual_message_report(
        self, interaction: Interaction, message: Message
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if message.author.bot:
            return await interaction.followup.send(
                content="Bot messages can't be reported."
            )

        if message.author == interaction.user:
            return await interaction.followup.send(
                content="Did you just report yourself?"
            )

        async with self._message_report_lock:
            reason = f"Manual report by {interaction.user.mention}"
            if not (components := await self._generate_message_report(message, reason)):
                return await interaction.followup.send(
                    content="Failed to report message. It may have already been reported or deleted."
                )

            warning, report, attachment = components
            reporter_is_reputable = isinstance(
                interaction.user, Member
            ) and is_reputable(interaction.user)

            report_channel = await self._get_report_channel()
            report_msg = await report_channel.send(
                embed=report,
                file=attachment,
                view=self._build_review_view(message) or discord_utils.MISSING,
            )

            moderator = await self.bot.get_or_fetch_user(
                cfg.rocketpool.support.moderator_id
            )

            confirm_view: WarningConfirmView | None = None
            if not reporter_is_reputable and self._sentinel.enabled:
                db_filter: dict[str, Any] = {
                    "type": "message",
                    "message_id": message.id,
                }

                async def on_automod_confirm() -> None:
                    await self._run_message_automod(message, reason, report_msg)

                async def on_warning_dismiss(moderator: Member) -> None:
                    async with self._message_report_lock:
                        report = await self.bot.db.scam_reports.find_one(db_filter)
                        if report is not None:
                            await self._resolve_report(
                                report["report_id"],
                                f"Marked safe by {moderator.mention}.",
                            )

                confirm_view = WarningConfirmView(
                    on_automod_confirm, on_warning_dismiss
                )

            warning_msg = await message.reply(
                content=f"{moderator.mention} {report_msg.jump_url}",
                embed=warning,
                view=confirm_view or discord_utils.MISSING,
                mention_author=False,
            )
            await self._add_message_report_to_db(
                message, reason, warning_msg, report_msg
            )

            if reporter_is_reputable:
                await self._run_message_automod(message, reason, report_msg)

            await interaction.followup.send(content="Thanks for reporting!")

    async def manual_user_report(self, interaction: Interaction, user: Member) -> None:
        await interaction.response.defer(ephemeral=True)

        if user.bot:
            return await interaction.followup.send(content="Bots can't be reported.")

        if user == interaction.user:
            return await interaction.followup.send(
                content="Did you just report yourself?"
            )

        async with self._user_report_lock:
            reason = f"Manual report by {interaction.user.mention}"
            if not (report := await self._generate_user_report(user, reason)):
                return await interaction.followup.send(
                    content="Failed to report user. They may have already been reported or banned."
                )

            report_channel = await self._get_report_channel()
            report_msg = await report_channel.send(embed=report)
            await self.bot.db.scam_reports.insert_one(
                {
                    "type": "user",
                    "guild_id": user.guild.id,
                    "user_id": user.id,
                    "reason": reason,
                    "content": user.display_name,
                    "warning_id": None,
                    "report_id": report_msg.id,
                }
            )
            await interaction.followup.send(content="Thanks for reporting!")

    # --- Reporting ---

    @staticmethod
    def _serialize_message(message: Message) -> str:
        data: dict[str, Any] = {"content": message.content}
        if message.embeds:
            data["embeds"] = [
                {"title": e.title, "description": e.description} for e in message.embeds
            ]
        return json.dumps(data, indent=2)

    @staticmethod
    def _build_automod_embed(report_msg: Message, actions: list[str]) -> Embed:
        description = ""
        if len(actions) > 1:
            description = ", ".join(actions[:-1]) + " and "
        description += actions[-1] + "."

        if not description.startswith("http"):
            # capitalize first letter, leave others untouched
            description = description[0].upper() + description[1:]

        return Embed(
            title=":hammer: Automated Moderation",
            url=report_msg.jump_url,
            color=ReportColor.ALERT,
            description=description,
        )

    async def report_message(self, message: Message, reason: str) -> None:
        async with self._message_report_lock:
            if not (components := await self._generate_message_report(message, reason)):
                return

            warning, report, attachment = components
            warning_msg = None

            try:
                warning_msg = await message.reply(
                    embed=warning,
                    mention_author=False,
                )
            except errors.Forbidden:
                log.warning(f"Failed to send warning message in reply to {message.id}")

            report_channel = await self._get_report_channel()
            report_msg = await report_channel.send(
                embed=report,
                file=attachment,
                view=self._build_review_view(message) or discord_utils.MISSING,
            )
            await self._add_message_report_to_db(
                message, reason, warning_msg, report_msg
            )

            await self._run_message_automod(message, reason, report_msg)

    async def report_thread(self, thread: Thread, reason: str) -> None:
        async with self._thread_report_lock:
            if not (components := await self._generate_thread_report(thread, reason)):
                return None

            warning, report = components

            try:
                warning_msg = await thread.send(embed=warning)
            except errors.Forbidden:
                log.warning(f"Failed to send warning message in thread {thread.id}")
                warning_msg = None

            report_channel = await self._get_report_channel()
            report_msg = await report_channel.send(
                embed=report,
                view=self._build_review_view(thread) or discord_utils.MISSING,
            )
            await self.bot.db.scam_reports.insert_one(
                {
                    "type": "thread",
                    "guild_id": thread.guild.id,
                    "channel_id": thread.id,
                    "user_id": thread.owner_id,
                    "reason": reason,
                    "content": thread.name,
                    "warning_id": warning_msg.id if warning_msg else None,
                    "report_id": report_msg.id,
                    "removed": False,
                }
            )

            await self._run_thread_automod(thread, reason, report_msg)

    # --- Automod ---

    async def _run_message_automod(
        self, message: Message, reason: str, report_msg: Message
    ) -> None:
        automod_message_channel: Any = message.channel
        alert_duration = MESSAGE_ALERT_DELETE_AFTER

        automod_actions = []
        timeout_duration = DEFAULT_USER_TIMEOUT
        try:
            if await self._sentinel.delete_message(message, reason):
                automod_actions.append("message deleted")
            if (
                isinstance(message.channel, Thread)
                and (message.channel.owner_id == message.author.id)
                and await self._sentinel.lock_thread(message.channel, reason)
            ):
                automod_actions.append(f"{message.channel.jump_url} locked")
                automod_message_channel = message.channel.parent
                alert_duration = THREAD_ALERT_DELETE_AFTER
            if isinstance(
                message.author, Member
            ) and await self._sentinel.timeout_member(
                message.author, int(timeout_duration.total_seconds()), reason
            ):
                duration = humanize.naturaldelta(timeout_duration)
                automod_actions.append(
                    f"{message.author.mention} timed out for {duration}"
                )
        except Exception as e:
            await self.bot.report_error(e)
            return

        if automod_actions and isinstance(automod_message_channel, Messageable):
            embed = self._build_automod_embed(report_msg, automod_actions)
            embed.set_footer(
                text=f"This alert will disappear in {humanize.naturaldelta(alert_duration)}."
            )
            await automod_message_channel.send(
                embed=embed, delete_after=alert_duration.total_seconds()
            )

    async def _run_thread_automod(
        self, thread: Thread, reason: str, report_msg: Message
    ) -> None:
        automod_actions = []
        timeout_duration = DEFAULT_USER_TIMEOUT
        alert_duration = THREAD_ALERT_DELETE_AFTER
        try:
            if await self._sentinel.lock_thread(thread, reason):
                automod_actions.append(f"{thread.jump_url} locked")
            if (
                thread.owner_id
                and (member := thread.guild.get_member(thread.owner_id))
                and await self._sentinel.timeout_member(
                    member, int(timeout_duration.total_seconds()), reason
                )
            ):
                duration = humanize.naturaldelta(timeout_duration)
                automod_actions.append(f"{member.mention} timed out for {duration}")
        except Exception as e:
            await self.bot.report_error(e)
            return

        if automod_actions and isinstance(thread.parent, Messageable):
            embed = self._build_automod_embed(report_msg, automod_actions)
            embed.set_footer(
                text=f"This alert will disappear in {humanize.naturaldelta(alert_duration)}."
            )
            await thread.parent.send(
                embed=embed, delete_after=alert_duration.total_seconds()
            )

    # --- Report generation ---

    async def _generate_message_report(
        self, message: Message, reason: str
    ) -> tuple[Embed, Embed, File] | None:
        try:
            message = await message.channel.fetch_message(message.id)
            if isinstance(message, DeletedReferencedMessage):
                return None
        except errors.NotFound:
            return None

        if await self.bot.db.scam_reports.find_one(
            {"type": "message", "message_id": message.id}
        ):
            log.info(f"Found existing report for message {message.id} in database")
            return None

        warning = Embed(title="🚨 Likely Scam Detected")
        warning.color = ReportColor.ALERT
        warning.description = f"**Reason**: {reason}\n"

        report = warning.copy()
        warning.set_footer(
            text=(
                "Do not engage with this user, action will be taken.\n"
                "Ignore any DMs you may receive."
            )
        )

        report.description = warning.description + (
            "\n"
            f"User ID:    `{message.author.id}` ({message.author.mention})\n"
            f"Message ID: `{message.id}` ({message.jump_url})\n"
            f"Channel ID: `{message.channel.id}` ({message.channel.jump_url})\n"
            "\n"
            "Original message has been attached as a file.\n"
            "Please review and take appropriate action."
        )

        attachment = TextFile(self._serialize_message(message), filename="message.json")
        return warning, report, attachment

    async def _generate_thread_report(
        self, thread: Thread, reason: str
    ) -> tuple[Embed, Embed] | None:
        if await self.bot.db.scam_reports.find_one(
            {"type": "thread", "channel_id": thread.id}
        ):
            log.info(f"Found existing report for thread {thread.id} in database")
            return None

        warning = Embed(title="🚨 Likely Scam Detected")
        warning.color = ReportColor.ALERT
        warning.description = f"**Reason**: {reason}\n"

        report = warning.copy()
        warning.set_footer(
            text=(
                "There is no ticket system for support on this server.\n"
                "Don't engage in conversation outside of the public #support channel.\n"
                "Ignore this thread and any invites or DMs you may receive."
            )
        )
        thread_owner = await self.bot.get_or_fetch_user(thread.owner_id)
        report.description = warning.description + (
            "\n"
            f"Thread Name: `{thread.name}`\n"
            f"Thread ID:   `{thread.id}` ({thread.jump_url})\n"
            f"User ID:     `{thread_owner.id}` ({thread_owner.mention})\n"
            "\n"
            "Please review and take appropriate action."
        )
        return warning, report

    async def _generate_user_report(self, user: Member, reason: str) -> Embed | None:
        if not isinstance(user, Member):
            return None

        if await self.bot.db.scam_reports.find_one(
            {"type": "user", "guild_id": user.guild.id, "user_id": user.id}
        ):
            log.info(f"Found existing report for user {user.id} in database")
            return None

        report = Embed(title="🚨 Suspicious User Detected")
        report.color = ReportColor.ALERT
        report.description = f"**Reason**: {reason}\n"
        report.description += (
            "\n"
            f"Name:  `{user.display_name}`\n"
            f"ID:    `{user.id}` ({user.mention})\n"
            f"Roles: [{', '.join(role.mention for role in user.roles[1:])}]\n"
            "\n"
            "Please review and take appropriate action."
        )
        report.set_thumbnail(url=user.display_avatar.url)
        return report

    # --- Helpers ---

    async def _on_message_delete(self, message_id: int) -> None:
        await self._check_thread_starter_deleted(message_id)
        async with self._message_report_lock:
            db_filter = {"type": "message", "message_id": message_id, "removed": False}
            if not (report := await self.bot.db.scam_reports.find_one(db_filter)):
                return

            channel = await self.bot.get_or_fetch_channel(report["channel_id"])
            assert isinstance(channel, Messageable)
            with contextlib.suppress(
                errors.NotFound, errors.Forbidden, errors.HTTPException
            ):
                message = await channel.fetch_message(report["warning_id"])
                await message.delete()

            await self._update_report(
                report["report_id"], "Original message has been deleted."
            )
            await self.bot.db.scam_reports.update_one(
                db_filter, {"$set": {"warning_id": None, "removed": True}}
            )

    async def _check_thread_starter_deleted(self, message_id: int) -> None:
        try:
            thread_id = self._thread_creation_messages.pop(message_id)
            thread = await self.bot.get_or_fetch_channel(thread_id)
        except (KeyError, errors.NotFound, errors.Forbidden):
            return

        if not isinstance(thread, Thread):
            return

        if await self._sentinel.is_banned(thread.guild.id, thread.owner_id):
            return  # owner already banned

        await self.report_thread(thread, "Attempt to hide thread from main channel")

    async def _update_report(self, report_msg_id: int, note: str) -> None:
        try:
            report_channel = await self._get_report_channel()
            message = await report_channel.fetch_message(report_msg_id)
            embed = message.embeds[0]
            embed.description = (embed.description or "") + f"\n\n**{note}**"
            embed.color = ReportColor.WARN
            await message.edit(embed=embed)
        except Exception as e:
            await self.bot.report_error(e)

    async def _resolve_report(self, report_msg_id: int, note: str) -> None:
        try:
            report_channel = await self._get_report_channel()
            message = await report_channel.fetch_message(report_msg_id)
            embed = message.embeds[0]
            embed.description = (embed.description or "") + f"\n\n**{note}**"
            embed.color = ReportColor.OK
            await message.edit(embed=embed, view=None)
        except Exception as e:
            await self.bot.report_error(e)

    async def _add_message_report_to_db(
        self,
        message: Message,
        reason: str,
        warning_msg: Message | None,
        report_msg: Message,
    ) -> None:
        await self.bot.db.scam_reports.insert_one(
            {
                "type": "message",
                "guild_id": message.guild.id if message.guild else None,
                "channel_id": message.channel.id,
                "message_id": message.id,
                "user_id": message.author.id,
                "reason": reason,
                "content": message.content,
                "embeds": [embed.to_dict() for embed in message.embeds],
                "warning_id": warning_msg.id if warning_msg else None,
                "report_id": report_msg.id,
                "removed": False,
            }
        )

    async def _increment_message_count(self, user: User | Member) -> int:
        result = await self.bot.db.message_counts.find_one_and_update(
            {"_id": user.id},
            {"$inc": {"count": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(result["count"]) if result else 0

    async def _get_report_channel(self) -> Messageable:
        channel = await self.bot.get_or_fetch_channel(
            cfg.discord.channels["report_scams"]
        )
        assert isinstance(channel, Messageable)
        return channel

    def _build_review_view(
        self, reportable: Message | Thread
    ) -> ReportReviewView | None:
        if not self._sentinel.enabled:
            return None

        if isinstance(reportable, Message):
            db_filter: dict[str, Any] = {
                "type": "message",
                "message_id": reportable.id,
            }
        else:
            db_filter = {"type": "thread", "channel_id": reportable.id}

        guild_id = (
            reportable.guild.id if reportable.guild else reportable.channel.guild.id  # type: ignore[union-attr]
        )

        async def on_confirm(moderator: Member) -> None:
            async with self._message_report_lock, self._thread_report_lock:
                if not (report := await self.bot.db.scam_reports.find_one(db_filter)):
                    return

                report_updates = [f"Confirmed by {moderator.mention}."]
                if await self._sentinel.ban_member(
                    moderator.guild.get_member(report["user_id"])
                    or await moderator.guild.fetch_member(report["user_id"]),
                    reason=report["reason"],
                ):
                    report_updates.append("- User has been banned.")
                else:
                    report_updates.append("- Failed to ban user.")

                await self._resolve_report(
                    report["report_id"], "\n".join(report_updates)
                )

        async def on_dismiss(moderator: Member) -> None:
            async with self._message_report_lock, self._thread_report_lock:
                if not (report := await self.bot.db.scam_reports.find_one(db_filter)):
                    return

                user_id = report["user_id"]
                report_updates = [f"Marked safe by {moderator.mention}."]

                if await self._sentinel.remove_timeout(
                    guild_id, user_id, "Report dismissed"
                ):
                    report_updates.append("- Timeout has been lifted.")

                if thread := moderator.guild.get_thread(report["channel_id"]):  # noqa: SIM102
                    if await self._sentinel.unlock_thread(thread, "Report dismissed"):
                        report_updates.append("- Thread has been unlocked.")

                await self._resolve_report(
                    report["report_id"], "\n".join(report_updates)
                )

        return ReportReviewView(on_confirm, on_dismiss)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(ScamDetection(bot))
