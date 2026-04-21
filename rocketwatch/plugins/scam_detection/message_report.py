from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

import humanize
from discord import (
    ButtonStyle,
    DeletedReferencedMessage,
    File,
    Interaction,
    Message,
    Thread,
    errors,
    ui,
)
from discord.abc import Messageable
from discord.utils import MISSING
from pymongo import ReturnDocument

from rocketwatch.plugins.scam_detection.common import (
    DEFAULT_USER_TIMEOUT,
    MESSAGE_ALERT_DELETE_AFTER,
    THREAD_ALERT_DELETE_AFTER,
    AutomodAction,
    ReportColor,
    ReportContext,
    build_automod_embed,
    get_report_channel,
    is_reputable,
    member_from_interaction,
    member_from_message,
    resolve_report,
    update_report,
)
from rocketwatch.plugins.scam_detection.views import ReportReviewView
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.file import TextFile

if TYPE_CHECKING:
    from rocketwatch.bot import RocketWatch
    from rocketwatch.plugins.scam_detection.scam_detection import ScamDetection

log = logging.getLogger("rocketwatch.scam_detection")


def _get_cog(interaction: Interaction[RocketWatch]) -> ScamDetection | None:
    return interaction.client.get_cog("ScamDetection")  # type: ignore[return-value]


class WarningConfirmView(ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _check_reputable(self, interaction: Interaction[RocketWatch]) -> bool:
        member = await member_from_interaction(interaction)
        if member and is_reputable(member):
            return True
        await interaction.response.send_message(
            content="Only moderators can confirm or dismiss reports.", ephemeral=True
        )
        return False

    @ui.button(label="Dismiss", style=ButtonStyle.danger, custom_id="warning:dismiss")
    async def dismiss(
        self,
        interaction: Interaction[RocketWatch],
        button: ui.Button[WarningConfirmView],
    ) -> None:
        if not await self._check_reputable(interaction):
            return
        assert interaction.message is not None
        if not (cog := _get_cog(interaction)):
            return
        warning_id = interaction.message.id
        await interaction.message.delete()

        if report := await interaction.client.db.scam_reports.find_one(
            {"warning_id": warning_id}
        ):
            await resolve_report(
                cog._ctx,
                report["report_id"],
                f"Marked safe by {interaction.user.mention}.",
            )

    @ui.button(label="Confirm", style=ButtonStyle.success, custom_id="warning:confirm")
    async def confirm(
        self,
        interaction: Interaction[RocketWatch],
        button: ui.Button[WarningConfirmView],
    ) -> None:
        if not await self._check_reputable(interaction):
            return
        assert interaction.message is not None
        if not (cog := _get_cog(interaction)):
            return
        warning_id = interaction.message.id
        await interaction.response.edit_message(view=None)

        if not (
            report := await interaction.client.db.scam_reports.find_one(
                {"warning_id": warning_id}
            )
        ):
            return
        channel = await interaction.client.get_or_fetch_channel(report["channel_id"])
        assert isinstance(channel, Messageable)
        try:
            message = await channel.fetch_message(report["message_id"])
        except (errors.NotFound, errors.Forbidden):
            return
        report_channel = await get_report_channel(cog._ctx)
        try:
            report_msg = await report_channel.fetch_message(report["report_id"])
        except (errors.NotFound, errors.Forbidden):
            return

        await run_message_automod(cog._ctx, message, report["reason"], report_msg)


def serialize_message(message: Message) -> str:
    data: dict[str, Any] = {"content": message.content}
    if message.embeds:
        data["embeds"] = [
            {"title": e.title, "description": e.description} for e in message.embeds
        ]
    return json.dumps(data, indent=2)


async def _claim_message_report(ctx: ReportContext, message_id: int) -> bool:
    """Atomically claim a slot for a message report. Returns True if claimed."""
    result = await ctx.bot.db.scam_reports.find_one_and_update(
        {"type": "message", "message_id": message_id},
        {"$setOnInsert": {"type": "message", "message_id": message_id}},
        upsert=True,
        return_document=ReturnDocument.BEFORE,
    )
    return result is None


async def _release_claim(ctx: ReportContext, message_id: int) -> None:
    """Remove a claimed placeholder if report creation fails."""
    await ctx.bot.db.scam_reports.delete_one(
        {"type": "message", "message_id": message_id}
    )


def _generate_embeds(message: Message, reason: str) -> tuple[Embed, Embed, File]:
    warning = Embed(
        title="🚨 Likely Scam Detected",
        description=f"**Reason**: {reason}",
        color=ReportColor.ALERT,
    )
    warning.set_footer(
        text=(
            "Do not engage with this user, action will be taken.\n"
            "Ignore any DMs you may receive."
        )
    )

    report = Embed(title="💬 Suspicious Message", color=ReportColor.ALERT)
    report.description = (
        f"**Reason**: {reason}\n\n"
        f"**User**: {message.author.mention}\n"
        f"**Message**: {message.jump_url}\n"
        f"**Channel**: {message.channel.jump_url}\n"
    )

    attachment = TextFile(serialize_message(message), filename="message.json")
    return warning, report, attachment


async def _finalize_report(
    ctx: ReportContext,
    message: Message,
    reason: str,
    warning_msg: Message | None,
    report_msg: Message,
) -> None:
    """Replace the claimed placeholder with the full report document."""
    await ctx.bot.db.scam_reports.replace_one(
        {"type": "message", "message_id": message.id},
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
            "message_deleted": False,
            "thread_removed": False,
        },
    )


def _build_review_view(ctx: ReportContext) -> ReportReviewView | None:
    if not ctx.sentinel.enabled:
        return None
    return ReportReviewView()


async def run_message_automod(
    ctx: ReportContext, message: Message, reason: str, report_msg: Message
) -> set[AutomodAction]:
    automod_message_channel: Any = message.channel
    alert_duration = MESSAGE_ALERT_DELETE_AFTER

    actions: set[AutomodAction] = set()
    action_descriptions: list[str] = []
    timeout_duration = DEFAULT_USER_TIMEOUT

    try:
        delete_request = ctx.sentinel.delete_message(message, reason)

        timeout_request = asyncio.sleep(0, result=False)
        if member := await member_from_message(ctx.bot, message):
            timeout_request = ctx.sentinel.timeout_member(
                member, int(timeout_duration.total_seconds()), reason
            )

        # message needs to be deleted before thread is locked
        deleted, timed_out = await asyncio.gather(delete_request, timeout_request)

        lock_request = asyncio.sleep(0, result=False)
        if (
            isinstance(message.channel, Thread)
            and message.channel.owner_id == message.author.id
        ):
            lock_request = ctx.sentinel.lock_thread(message.channel, reason)

        locked = await lock_request

        if deleted:
            actions.add(AutomodAction.MESSAGE_DELETED)
            action_descriptions.append("message deleted")
        if locked:
            actions.add(AutomodAction.THREAD_LOCKED)
            action_descriptions.append(f"{message.channel.jump_url} locked")
            assert isinstance(message.channel, Thread)
            automod_message_channel = message.channel.parent
            alert_duration = THREAD_ALERT_DELETE_AFTER
        if timed_out:
            actions.add(AutomodAction.MEMBER_TIMED_OUT)
            duration = humanize.naturaldelta(timeout_duration)
            action_descriptions.append(
                f"{message.author.mention} timed out for {duration}"
            )
    except Exception as e:
        await ctx.bot.report_error(e)
        return actions

    if action_descriptions and isinstance(automod_message_channel, Messageable):
        embed = build_automod_embed(report_msg, action_descriptions)
        embed.set_footer(
            text=f"This alert will disappear in {humanize.naturaldelta(alert_duration)}."
        )
        await automod_message_channel.send(
            embed=embed, delete_after=alert_duration.total_seconds()
        )

    return actions


async def report_message(ctx: ReportContext, message: Message, reason: str) -> None:
    try:
        message = await message.channel.fetch_message(message.id)
        if isinstance(message, DeletedReferencedMessage):
            return
    except errors.NotFound:
        return

    if not await _claim_message_report(ctx, message.id):
        log.info(f"Found existing report for message {message.id} in database")
        return

    try:
        warning, report, attachment = _generate_embeds(message, reason)

        report_channel = await get_report_channel(ctx)
        report_msg = await report_channel.send(
            embed=report,
            file=attachment,
            view=_build_review_view(ctx) or MISSING,
        )
        await _finalize_report(ctx, message, reason, None, report_msg)
    except Exception:
        await _release_claim(ctx, message.id)
        raise

    actions = await run_message_automod(ctx, message, reason, report_msg)

    if AutomodAction.MESSAGE_DELETED not in actions:
        warning.url = report_msg.jump_url
        try:
            warning_msg = await message.reply(
                embed=warning,
                mention_author=False,
            )
        except (errors.NotFound, errors.Forbidden):
            log.warning(f"Failed to send warning message in reply to {message.id}")
        else:
            await ctx.bot.db.scam_reports.update_one(
                {"type": "message", "message_id": message.id},
                {"$set": {"warning_id": warning_msg.id}},
            )


async def manual_message_report(
    ctx: ReportContext, interaction: Interaction[RocketWatch], message: Message
) -> None:
    await interaction.response.defer(ephemeral=True)

    if message.author.bot:
        return await interaction.followup.send(
            content="Bot messages can't be reported."
        )

    if message.author == interaction.user:
        return await interaction.followup.send(content="Did you just report yourself?")

    try:
        message = await message.channel.fetch_message(message.id)
        if isinstance(message, DeletedReferencedMessage):
            return await interaction.followup.send(
                content="Failed to report message. It may have already been reported or deleted."
            )
    except errors.NotFound:
        return await interaction.followup.send(
            content="Failed to report message. It may have already been reported or deleted."
        )

    if not await _claim_message_report(ctx, message.id):
        return await interaction.followup.send(
            content="Failed to report message. It may have already been reported or deleted."
        )

    try:
        reason = f"Manual report by {interaction.user.mention}"
        warning, report, attachment = _generate_embeds(message, reason)

        report_channel = await get_report_channel(ctx)
        report_msg = await report_channel.send(
            embed=report,
            file=attachment,
            view=_build_review_view(ctx) or MISSING,
        )

        moderator = await ctx.bot.get_or_fetch_user(cfg.rocketpool.support.moderator_id)
        warning.url = report_msg.jump_url

        reporter = await member_from_interaction(interaction)
        if reporter_is_reputable := (reporter and is_reputable(reporter)):
            await _finalize_report(ctx, message, reason, None, report_msg)
        else:
            confirm_view: WarningConfirmView | None = None
            if ctx.sentinel.enabled:
                confirm_view = WarningConfirmView()

            warning_msg: Message | None = None
            with contextlib.suppress(Exception):
                warning_msg = await message.reply(
                    content=moderator.mention,
                    embed=warning,
                    view=confirm_view or MISSING,
                    mention_author=False,
                )
            await _finalize_report(ctx, message, reason, warning_msg, report_msg)
    except Exception:
        await _release_claim(ctx, message.id)
        raise

    if reporter_is_reputable:
        actions = await run_message_automod(ctx, message, reason, report_msg)
        if AutomodAction.MESSAGE_DELETED not in actions:
            try:
                warning_msg = await message.reply(
                    content=f"{moderator.mention} {report_msg.jump_url}",
                    embed=warning,
                    mention_author=False,
                )
            except (errors.NotFound, errors.Forbidden):
                log.warning(f"Failed to send warning message in reply to {message.id}")
            else:
                await ctx.bot.db.scam_reports.update_one(
                    {"type": "message", "message_id": message.id},
                    {"$set": {"warning_id": warning_msg.id}},
                )

    await interaction.followup.send(content="Thanks for reporting!")


async def on_message_delete(ctx: ReportContext, message_id: int) -> None:
    report = await ctx.bot.db.scam_reports.find_one_and_update(
        {"type": "message", "message_id": message_id, "message_deleted": False},
        {"$set": {"message_deleted": True, "warning_id": None}},
    )
    if not report:
        return

    channel = await ctx.bot.get_or_fetch_channel(report["channel_id"])
    assert isinstance(channel, Messageable)
    with contextlib.suppress(errors.NotFound, errors.Forbidden, errors.HTTPException):
        warning = await channel.fetch_message(report["warning_id"])
        await warning.delete()

    await update_report(ctx, report["report_id"], "Original message has been deleted.")
