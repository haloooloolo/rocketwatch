import contextlib
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

from discord import ButtonStyle, Color, Interaction, Member, Thread, errors, ui
from discord.abc import Messageable

from rocketwatch.utils.config import cfg

if TYPE_CHECKING:
    from rocketwatch.bot import RocketWatch
    from rocketwatch.plugins.scam_detection.scam_detection import ScamDetection

log = logging.getLogger("rocketwatch.scam_detection")


def is_reputable(user: Member) -> bool:
    return any(
        (
            user.id == cfg.discord.owner.user_id,
            user.id in cfg.rocketpool.support.user_ids,
            {role.id for role in user.roles} & set(cfg.rocketpool.support.role_ids),
            user.guild_permissions.moderate_members,
        )
    )


def _get_cog(interaction: Interaction["RocketWatch"]) -> "ScamDetection | None":
    return interaction.client.get_cog("ScamDetection")  # type: ignore[return-value]


@contextlib.asynccontextmanager
async def _report_locks(cog: "ScamDetection", report_type: str) -> AsyncIterator[None]:
    if report_type in ("message", "thread"):
        async with cog._message_report_lock, cog._thread_report_lock:
            yield
    else:
        async with cog._user_report_lock:
            yield


class ReportColor:
    ALERT = Color.from_rgb(255, 0, 0)
    WARN = Color.from_rgb(255, 165, 0)
    OK = Color.from_rgb(0, 255, 0)


class ScamReport(TypedDict):
    type: str
    guild_id: int | None
    user_id: int
    reason: str
    content: str | None
    warning_id: int | None
    report_id: int
    user_banned: bool
    channel_id: NotRequired[int]
    message_id: NotRequired[int]
    embeds: NotRequired[list[dict[str, Any]]]
    removed: NotRequired[bool]


class WarningConfirmView(ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _check_reputable(self, interaction: Interaction["RocketWatch"]) -> bool:
        if isinstance(interaction.user, Member) and is_reputable(interaction.user):
            return True
        await interaction.response.send_message(
            content="Only moderators can confirm or dismiss reports.", ephemeral=True
        )
        return False

    @ui.button(label="Confirm", style=ButtonStyle.danger, custom_id="warning:confirm")
    async def confirm(
        self,
        interaction: Interaction["RocketWatch"],
        button: ui.Button["WarningConfirmView"],
    ) -> None:
        if not await self._check_reputable(interaction):
            return
        assert interaction.message is not None
        if not (cog := _get_cog(interaction)):
            return
        warning_id = interaction.message.id
        await interaction.response.edit_message(view=None)
        async with cog._message_report_lock:
            if not (
                report := await interaction.client.db.scam_reports.find_one(
                    {"warning_id": warning_id}
                )
            ):
                return
            channel = await interaction.client.get_or_fetch_channel(
                report["channel_id"]
            )
            assert isinstance(channel, Messageable)
            try:
                message = await channel.fetch_message(report["message_id"])
            except (errors.NotFound, errors.Forbidden):
                return
            report_channel = await cog._get_report_channel()
            try:
                report_msg = await report_channel.fetch_message(report["report_id"])
            except (errors.NotFound, errors.Forbidden):
                return
            await cog._run_message_automod(message, report["reason"], report_msg)

    @ui.button(label="Dismiss", style=ButtonStyle.success, custom_id="warning:dismiss")
    async def dismiss(
        self,
        interaction: Interaction["RocketWatch"],
        button: ui.Button["WarningConfirmView"],
    ) -> None:
        if not await self._check_reputable(interaction):
            return
        assert isinstance(interaction.user, Member)
        assert interaction.message is not None
        if not (cog := _get_cog(interaction)):
            return
        warning_id = interaction.message.id
        await interaction.message.delete()
        async with cog._message_report_lock:
            if report := await interaction.client.db.scam_reports.find_one(
                {"warning_id": warning_id}
            ):
                await cog._resolve_report(
                    report["report_id"],
                    f"Marked safe by {interaction.user.mention}.",
                )


class ReportReviewView(ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @ui.button(
        label="Confirm Scam", style=ButtonStyle.danger, custom_id="report:confirm"
    )
    async def confirm(
        self,
        interaction: Interaction["RocketWatch"],
        button: ui.Button["ReportReviewView"],
    ) -> None:
        if not (
            isinstance(interaction.user, Member)
            and interaction.user.guild_permissions.ban_members
        ):
            await interaction.response.send_message(
                content="Only admins can confirm reports.", ephemeral=True
            )
            return
        assert isinstance(interaction.user, Member)
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
        async with _report_locks(cog, report["type"]):
            if not (
                report := await interaction.client.db.scam_reports.find_one(
                    {"report_id": report_id}
                )
            ):
                return
            if not (
                reported_member := await interaction.client.get_or_fetch_member(
                    interaction.user.guild.id, report["user_id"]
                )
            ):
                return
            updates = [f"Confirmed by {interaction.user.mention}."]
            if await cog._sentinel.ban_member(reported_member, reason=report["reason"]):
                updates.append("- User has been banned.")
            else:
                updates.append("- Failed to ban user.")
            await cog._resolve_report(report_id, "\n".join(updates))

    @ui.button(label="Mark Safe", style=ButtonStyle.success, custom_id="report:dismiss")
    async def dismiss(
        self,
        interaction: Interaction["RocketWatch"],
        button: ui.Button["ReportReviewView"],
    ) -> None:
        if not (
            isinstance(interaction.user, Member)
            and interaction.user.guild_permissions.moderate_members
        ):
            await interaction.response.send_message(
                content="Only moderators can dismiss reports.", ephemeral=True
            )
            return
        assert isinstance(interaction.user, Member)
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
        async with _report_locks(cog, report["type"]):
            if not (
                report := await interaction.client.db.scam_reports.find_one(
                    {"report_id": report_id}
                )
            ):
                return

            guild_id = report.get("guild_id") or interaction.user.guild.id
            updates = [f"Marked safe by {interaction.user.mention}."]

            if await cog._sentinel.remove_timeout(
                guild_id, report["user_id"], "Report dismissed"
            ):
                updates.append("- Timeout has been lifted.")

            if report["type"] in ("message", "thread"):
                if channel_id := report.get("channel_id"):
                    channel = await interaction.client.get_or_fetch_channel(channel_id)
                    if isinstance(
                        channel, Thread
                    ) and await cog._sentinel.unlock_thread(
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

            await cog._resolve_report(report_id, "\n".join(updates))
