from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from discord import ButtonStyle, Interaction, Thread, errors, ui
from discord.abc import Messageable

from rocketwatch.plugins.scam_detection.common import (
    is_reputable,
    member_from_interaction,
    resolve_report,
)

if TYPE_CHECKING:
    from rocketwatch.bot import RocketWatch
    from rocketwatch.plugins.scam_detection.scam_detection import ScamDetection

log = logging.getLogger("rocketwatch.scam_detection")


def _get_cog(interaction: Interaction[RocketWatch]) -> ScamDetection | None:
    return interaction.client.get_cog("ScamDetection")  # type: ignore[return-value]


class ReportReviewView(ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @ui.button(label="Mark Safe", style=ButtonStyle.success, custom_id="report:dismiss")
    async def dismiss(
        self,
        interaction: Interaction[RocketWatch],
        _button: ui.Button[ReportReviewView],
    ) -> None:
        member = await member_from_interaction(interaction)
        if not (member and is_reputable(member)):
            await interaction.response.send_message(
                content="Only moderators can dismiss reports.", ephemeral=True
            )
            return
        assert interaction.message is not None
        if not (cog := _get_cog(interaction)):
            return
        report_id = interaction.message.id
        await interaction.response.edit_message(view=None)

        if not (
            report := await interaction.client.db.scam_reports.find_one(
                {"report_id": report_id}
            )
        ):
            return

        guild_id = report.get("guild_id") or member.guild.id
        updates = [f"Marked safe by {interaction.user.mention}."]

        if await cog._ctx.sentinel.remove_timeout(
            guild_id, report["user_id"], "Report dismissed"
        ):
            updates.append("- Timeout has been lifted.")

        if report["type"] in ("message", "thread"):
            if channel_id := report.get("channel_id"):
                channel = await interaction.client.get_or_fetch_channel(channel_id)
                if isinstance(
                    channel, Thread
                ) and await cog._ctx.sentinel.unlock_thread(
                    channel, "Report dismissed"
                ):
                    updates.append("- Thread has been unlocked.")

            if warning_id := report.get("warning_id"):
                channel = await interaction.client.get_or_fetch_channel(
                    report["channel_id"]
                )
                if isinstance(channel, Messageable):
                    with contextlib.suppress(
                        errors.NotFound, errors.Forbidden, errors.HTTPException
                    ):
                        warning_msg = await channel.fetch_message(warning_id)
                        await warning_msg.delete()

        await resolve_report(cog._ctx, report_id, "\n".join(updates))

    @ui.button(
        label="Confirm Scam", style=ButtonStyle.danger, custom_id="report:confirm"
    )
    async def confirm(
        self,
        interaction: Interaction[RocketWatch],
        _button: ui.Button[ReportReviewView],
    ) -> None:
        member = await member_from_interaction(interaction)
        if not (member and is_reputable(member)):
            await interaction.response.send_message(
                content="Only admins can confirm reports.", ephemeral=True
            )
            return
        assert interaction.message is not None
        if not (cog := _get_cog(interaction)):
            return
        report_id = interaction.message.id
        await interaction.response.edit_message(view=None)

        if not (
            report := await interaction.client.db.scam_reports.find_one(
                {"report_id": report_id}
            )
        ):
            return
        if not (
            reported_member := await interaction.client.get_or_fetch_member(
                member.guild.id, report["user_id"]
            )
        ):
            return
        updates = [f"Confirmed by {interaction.user.mention}."]
        if await cog._ctx.sentinel.ban_member(reported_member, reason=report["reason"]):
            updates.append("- User has been banned.")
        else:
            updates.append("- Failed to ban user.")
        await resolve_report(cog._ctx, report_id, "\n".join(updates))
