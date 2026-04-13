import logging

from discord import Interaction, Member, Message
from discord.utils import MISSING, format_dt
from pymongo import ReturnDocument

from rocketwatch.plugins.scam_detection.common import (
    DEFAULT_USER_TIMEOUT,
    ReportColor,
    ReportContext,
    get_report_channel,
    is_reputable,
)
from rocketwatch.plugins.scam_detection.views import ReportReviewView
from rocketwatch.utils.embeds import Embed

log = logging.getLogger("rocketwatch.scam_detection")


async def _claim_user_report(ctx: ReportContext, guild_id: int, user_id: int) -> bool:
    """Atomically claim a slot for a user report. Returns True if claimed."""
    result = await ctx.bot.db.scam_reports.find_one_and_update(
        {"type": "user", "guild_id": guild_id, "user_id": user_id},
        {"$setOnInsert": {"type": "user", "guild_id": guild_id, "user_id": user_id}},
        upsert=True,
        return_document=ReturnDocument.BEFORE,
    )
    return result is None


async def _release_claim(ctx: ReportContext, guild_id: int, user_id: int) -> None:
    """Remove a claimed placeholder if report creation fails."""
    await ctx.bot.db.scam_reports.delete_one(
        {"type": "user", "guild_id": guild_id, "user_id": user_id}
    )


async def _finalize_report(
    ctx: ReportContext,
    user: Member,
    reason: str,
    report_msg: Message,
) -> None:
    """Replace the claimed placeholder with the full report document."""
    await ctx.bot.db.scam_reports.replace_one(
        {"type": "user", "guild_id": user.guild.id, "user_id": user.id},
        {
            "type": "user",
            "guild_id": user.guild.id,
            "user_id": user.id,
            "reason": reason,
            "content": user.display_name,
            "warning_id": None,
            "report_id": report_msg.id,
        },
    )


def _generate_report_embed(user: Member, reason: str) -> Embed:
    report = Embed(title="👤 Suspicious User")
    report.color = ReportColor.ALERT
    report.description = f"**Reason**: {reason}\n"
    report.description += (
        "\n"
        f"User: `{user.display_name}` ({user.mention})\n"
        f"Created: {format_dt(user.created_at, 'R')}\n"
    )
    if user.joined_at:
        report.description += f"Joined: {format_dt(user.joined_at, 'R')}\n"
    report.description += (
        f"Roles: [{', '.join(role.mention for role in user.roles[1:])}]\n"
    )
    report.set_thumbnail(url=user.display_avatar.url)
    return report


def _build_review_view(ctx: ReportContext) -> ReportReviewView | None:
    if not ctx.sentinel.enabled:
        return None
    return ReportReviewView()


async def run_user_automod(
    ctx: ReportContext, member: Member, reason: str, report_msg: Message
) -> None:
    timeout_duration = DEFAULT_USER_TIMEOUT
    try:
        await ctx.sentinel.timeout_member(
            member, int(timeout_duration.total_seconds()), reason
        )
    except Exception as e:
        await ctx.bot.report_error(e)


async def manual_user_report(
    ctx: ReportContext, interaction: Interaction, user: Member
) -> None:
    await interaction.response.defer(ephemeral=True)

    if user.bot:
        return await interaction.followup.send(content="Bots can't be reported.")

    if user == interaction.user:
        return await interaction.followup.send(content="Did you just report yourself?")

    if not isinstance(user, Member):
        return await interaction.followup.send(
            content="Failed to report user. They may have already been reported or banned."
        )

    if not await _claim_user_report(ctx, user.guild.id, user.id):
        return await interaction.followup.send(
            content="Failed to report user. They may have already been reported or banned."
        )

    try:
        reason = f"Manual report by {interaction.user.mention}"
        report = _generate_report_embed(user, reason)

        report_channel = await get_report_channel(ctx)
        report_msg = await report_channel.send(
            embed=report,
            view=_build_review_view(ctx) or MISSING,
        )
        await _finalize_report(ctx, user, reason, report_msg)
    except Exception:
        await _release_claim(ctx, user.guild.id, user.id)
        raise

    reporter_is_reputable = isinstance(interaction.user, Member) and is_reputable(
        interaction.user
    )
    if reporter_is_reputable:
        await run_user_automod(ctx, user, reason, report_msg)

    await interaction.followup.send(content="Thanks for reporting!")
