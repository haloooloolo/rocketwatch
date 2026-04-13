import asyncio
import logging

import humanize
from discord import Message, Thread, errors
from discord.abc import Messageable
from discord.utils import MISSING, format_dt
from pymongo import ReturnDocument

from rocketwatch.plugins.scam_detection.common import (
    DEFAULT_USER_TIMEOUT,
    THREAD_ALERT_DELETE_AFTER,
    ReportColor,
    ReportContext,
    build_automod_embed,
    get_report_channel,
    update_report,
)
from rocketwatch.plugins.scam_detection.views import ReportReviewView
from rocketwatch.utils.embeds import Embed

log = logging.getLogger("rocketwatch.scam_detection")


def _generate_embeds(thread: Thread, reason: str) -> tuple[Embed, Embed]:
    warning = Embed(
        title="🚨 Likely Scam Detected",
        description=f"**Reason**: {reason}",
        color=ReportColor.ALERT,
    )
    warning.set_footer(
        text=(
            "There is no ticket system for support on this server.\n"
            "Don't engage in conversation outside of the public #support channel.\n"
            "Ignore this thread and any invites or DMs you may receive."
        )
    )

    report = Embed(
        title="🧵 Suspicious Thread",
        description=f"**Reason**: {reason}\n",
        color=ReportColor.ALERT,
    )

    return warning, report


async def _claim_thread_report(ctx: ReportContext, thread_id: int) -> bool:
    """Atomically claim a slot for a thread report. Returns True if claimed."""
    result = await ctx.bot.db.scam_reports.find_one_and_update(
        {"type": "thread", "channel_id": thread_id},
        {"$setOnInsert": {"type": "thread", "channel_id": thread_id}},
        upsert=True,
        return_document=ReturnDocument.BEFORE,
    )
    return result is None


async def _release_claim(ctx: ReportContext, thread_id: int) -> None:
    """Remove a claimed placeholder if report creation fails."""
    await ctx.bot.db.scam_reports.delete_one(
        {"type": "thread", "channel_id": thread_id}
    )


async def _finalize_report(
    ctx: ReportContext,
    thread: Thread,
    reason: str,
    warning_msg: Message | None,
    report_msg: Message,
) -> None:
    """Replace the claimed placeholder with the full report document."""
    await ctx.bot.db.scam_reports.replace_one(
        {"type": "thread", "channel_id": thread.id},
        {
            "type": "thread",
            "guild_id": thread.guild.id,
            "channel_id": thread.id,
            "user_id": thread.owner_id,
            "reason": reason,
            "content": thread.name,
            "warning_id": warning_msg.id if warning_msg else None,
            "report_id": report_msg.id,
            "thread_removed": False,
        },
    )


def _build_review_view(ctx: ReportContext) -> ReportReviewView | None:
    if not ctx.sentinel.enabled:
        return None
    return ReportReviewView()


async def run_thread_automod(
    ctx: ReportContext, thread: Thread, reason: str, report_msg: Message
) -> None:
    automod_actions = []
    timeout_duration = DEFAULT_USER_TIMEOUT
    alert_duration = THREAD_ALERT_DELETE_AFTER

    try:
        if thread.owner_id and (member := thread.guild.get_member(thread.owner_id)):
            timeout_request = ctx.sentinel.timeout_member(
                member, int(timeout_duration.total_seconds()), reason
            )
        else:
            timeout_request = asyncio.sleep(0, result=False)

        lock_request = ctx.sentinel.lock_thread(thread, reason)

        timed_out, locked = await asyncio.gather(timeout_request, lock_request)

        if locked:
            automod_actions.append(f"{thread.jump_url} locked")
        if timed_out:
            assert member is not None
            duration = humanize.naturaldelta(timeout_duration)
            automod_actions.append(f"{member.mention} timed out for {duration}")
    except Exception as e:
        await ctx.bot.report_error(e)
        return

    if automod_actions and isinstance(thread.parent, Messageable):
        embed = build_automod_embed(report_msg, automod_actions)
        embed.set_footer(
            text=f"This alert will disappear in {humanize.naturaldelta(alert_duration)}."
        )
        await thread.parent.send(
            embed=embed, delete_after=alert_duration.total_seconds()
        )


async def report_thread(ctx: ReportContext, thread: Thread, reason: str) -> None:
    if not await _claim_thread_report(ctx, thread.id):
        log.info(f"Found existing report for thread {thread.id} in database")
        return

    try:
        thread_owner = await ctx.bot.get_or_fetch_user(thread.owner_id)
        warning, report = _generate_embeds(thread, reason)
        report.description = (report.description or "") + (
            "\n"
            f"Thread: `{thread.name}` ({thread.jump_url})\n"
            f"User: {thread_owner.mention}\n"
        )
        if thread.created_at:
            report.description += f"Created: {format_dt(thread.created_at, 'R')}\n"
        report.description += f"Messages: {thread.message_count}\n"
        report.description += f"Members: {thread.member_count}\n"

        try:
            warning_msg = await thread.send(embed=warning)
        except errors.Forbidden:
            log.warning(f"Failed to send warning message in thread {thread.id}")
            warning_msg = None

        report_channel = await get_report_channel(ctx)
        report_msg = await report_channel.send(
            embed=report,
            view=_build_review_view(ctx) or MISSING,
        )
        await _finalize_report(ctx, thread, reason, warning_msg, report_msg)
    except Exception:
        await _release_claim(ctx, thread.id)
        raise

    await run_thread_automod(ctx, thread, reason, report_msg)


async def on_thread_removed(ctx: ReportContext, thread_id: int, note: str) -> None:
    """Atomically claim and process thread removal events (lock or delete)."""
    while report := await ctx.bot.db.scam_reports.find_one_and_update(
        {"channel_id": thread_id, "thread_removed": False},
        {"$set": {"thread_removed": True}},
    ):
        if report.get("type") == "thread":
            await ctx.bot.db.scam_reports.update_one(
                {"_id": report["_id"]}, {"$set": {"warning_id": None}}
            )
        await update_report(ctx, report["report_id"], note)


async def check_thread_starter_deleted(
    ctx: ReportContext,
    message_id: int,
    thread_creation_messages: dict[int, int],
) -> None:
    try:
        thread_id = thread_creation_messages.pop(message_id)
        thread = await ctx.bot.get_or_fetch_channel(thread_id)
    except (KeyError, errors.NotFound, errors.Forbidden):
        return

    if not isinstance(thread, Thread):
        return

    if await ctx.sentinel.is_banned(thread.guild.id, thread.owner_id):
        return  # owner already banned

    await report_thread(ctx, thread, "Attempt to hide thread from main channel")
