from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord import (
    Forbidden,
    Guild,
    Interaction,
    Member,
    Message,
    NotFound,
    TextStyle,
    User,
    ui,
)
from discord.utils import MISSING, format_dt
from pymongo import ReturnDocument

from rocketwatch.plugins.scam_detection.common import (
    DEFAULT_USER_TIMEOUT,
    AutomodAction,
    ReportColor,
    ReportContext,
    get_report_channel,
    is_reputable,
    member_from_interaction,
)
from rocketwatch.plugins.scam_detection.partner_sync import broadcast_user_report
from rocketwatch.plugins.scam_detection.views import ReportReviewView
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import Embed

if TYPE_CHECKING:
    from rocketwatch.bot import RocketWatch

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
        f"**User**: `{user.display_name}` ({user.mention})\n"
        f"**Created**: {format_dt(user.created_at, 'R')}\n"
    )
    if user.joined_at:
        report.description += f"**Joined**: {format_dt(user.joined_at, 'R')}\n"
    report.description += (
        f"**Roles**: [{', '.join(role.mention for role in user.roles[1:])}]\n"
    )
    report.set_thumbnail(url=user.display_avatar.url)
    return report


def _build_review_view(ctx: ReportContext) -> ReportReviewView | None:
    if not ctx.sentinel.enabled:
        return None
    return ReportReviewView()


async def run_user_automod(
    ctx: ReportContext, member: Member, reason: str
) -> set[AutomodAction]:
    actions: set[AutomodAction] = set()
    timeout_duration = DEFAULT_USER_TIMEOUT
    try:
        timed_out = await ctx.sentinel.timeout_member(
            member, int(timeout_duration.total_seconds()), reason
        )
    except Exception as e:
        await ctx.bot.report_error(e)
        return actions

    if timed_out:
        actions.add(AutomodAction.MEMBER_TIMED_OUT)

    return actions


def _compose_manual_reason(interaction: Interaction[RocketWatch], note: str) -> str:
    return f"{note} (reported by {interaction.user.mention})"


class UserReportReasonModal(ui.Modal, title="Report User"):
    def __init__(self, ctx: ReportContext, user: Member) -> None:
        super().__init__()
        self._ctx = ctx
        self._user = user
        self.reason_field: ui.TextInput[UserReportReasonModal] = ui.TextInput(
            label="Reason",
            placeholder="Why are you reporting this user?",
            style=TextStyle.paragraph,
            required=True,
            max_length=250,
        )
        self.add_item(self.reason_field)

    async def on_submit(self, interaction: Interaction[RocketWatch]) -> None:  # type: ignore[override]
        await _execute_user_report(
            self._ctx, interaction, self._user, self.reason_field.value.strip()
        )


async def manual_user_report(
    ctx: ReportContext,
    interaction: Interaction[RocketWatch],
    user: Member,
    reason: str = "",
) -> None:
    if user.bot:
        await interaction.response.send_message(
            content="Bots can't be reported.", ephemeral=True
        )
        return

    if user == interaction.user:
        await interaction.response.send_message(
            content="Did you just report yourself?", ephemeral=True
        )
        return

    if not isinstance(user, Member):
        await interaction.response.send_message(
            content="Failed to report user. They may have already been reported or banned.",
            ephemeral=True,
        )
        return

    if not reason:
        await interaction.response.send_modal(UserReportReasonModal(ctx, user))
        return

    await _execute_user_report(ctx, interaction, user, reason)


async def _execute_user_report(
    ctx: ReportContext,
    interaction: Interaction[RocketWatch],
    user: Member,
    note: str,
) -> None:
    await interaction.response.defer(ephemeral=True)

    if not await _claim_user_report(ctx, user.guild.id, user.id):
        return await interaction.followup.send(
            content="Failed to report user. They may have already been reported or banned."
        )

    try:
        reason = _compose_manual_reason(interaction, note)
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

    await broadcast_user_report(ctx, user.id, report_msg)

    reporter = await member_from_interaction(interaction)
    if reporter and is_reputable(reporter):
        await run_user_automod(ctx, user, reason)

    await interaction.followup.send(content="Thanks for reporting!")


async def report_user_from_partner_ban(
    ctx: ReportContext, partner_guild: Guild, banned_user: User
) -> None:
    """Create a user report in the RP guild when a partner server bans a user
    who is also a member of RP. No auto-action; surfaces to mods for review."""
    if banned_user.bot:
        return

    rp_guild_id = cfg.rocketpool.support.server_id
    try:
        member = await ctx.bot.get_or_fetch_member(rp_guild_id, banned_user.id)
    except (NotFound, Forbidden):
        return
    except Exception as e:
        log.warning(
            f"Failed to look up banned user {banned_user.id} in RP guild: {e!r}"
        )
        return

    if is_reputable(member):
        return

    if not await _claim_user_report(ctx, rp_guild_id, banned_user.id):
        return

    try:
        reason = f"Banned in partner server `{partner_guild.name}`"
        report = _generate_report_embed(member, reason)

        report_channel = await get_report_channel(ctx)
        report_msg = await report_channel.send(
            embed=report,
            view=_build_review_view(ctx) or MISSING,
        )
        await _finalize_report(ctx, member, reason, report_msg)
    except Exception:
        await _release_claim(ctx, rp_guild_id, banned_user.id)
        raise
