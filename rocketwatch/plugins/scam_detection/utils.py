import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

from discord import ButtonStyle, Color, Interaction, Member, Message, Thread, ui

from rocketwatch.utils.config import cfg

if TYPE_CHECKING:
    from rocketwatch.bot import RocketWatch

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


class RemovalVoteView(ui.View):
    THRESHOLD = 5

    def __init__(
        self,
        reportable: Message | Thread,
        on_mark_safe: Callable[[str], Awaitable[None]],
    ):
        super().__init__(timeout=None)
        self.reportable = reportable
        self._on_mark_safe = on_mark_safe
        self.safu_votes: set[int] = set()

    @ui.button(label="Mark Safu", style=ButtonStyle.blurple)
    async def mark_safe(
        self,
        interaction: Interaction["RocketWatch"],
        button: ui.Button["RemovalVoteView"],
    ) -> None:
        if interaction.message is None:
            return

        log.info(
            f"User {interaction.user.id} marked message {interaction.message.id} as safe"
        )

        reportable_repr = type(self.reportable).__name__.lower()
        if interaction.user.id in self.safu_votes:
            log.debug(f"User {interaction.user.id} already voted on {reportable_repr}")
            await interaction.response.send_message(
                content="You already voted!", ephemeral=True
            )
            return

        if isinstance(interaction.user, Member) and interaction.user.is_timed_out():
            log.debug(
                f"Timed-out user {interaction.user.id} tried to vote on {self.reportable}"
            )
            return

        reported_user = None
        if isinstance(self.reportable, Message):
            reported_user = self.reportable.author
        elif isinstance(self.reportable, Thread):
            reported_user = self.reportable.owner

        if interaction.user == reported_user:
            log.debug(
                f"User {interaction.user.id} tried to mark their own {reportable_repr} as safe"
            )
            await interaction.response.send_message(
                content=f"You can't vote on your own {reportable_repr}!",
                ephemeral=True,
            )
            return

        self.safu_votes.add(interaction.user.id)

        if isinstance(interaction.user, Member) and is_reputable(interaction.user):
            user_repr = interaction.user.mention
        elif len(self.safu_votes) >= self.THRESHOLD:
            user_repr = "the community"
        else:
            button.label = f"Mark Safu ({len(self.safu_votes)}/{self.THRESHOLD})"
            await interaction.response.edit_message(view=self)
            return

        await interaction.message.delete()
        await self._on_mark_safe(user_repr)
        await interaction.response.send_message(
            content="Warning removed!", ephemeral=True
        )
