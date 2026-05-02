import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Any, TypedDict

from discord import Interaction, Member, Message
from discord.abc import Messageable

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import CustomColors, Embed
from rocketwatch.utils.sentinel import SentinelClient

log = logging.getLogger("rocketwatch.scam_detection")

REPUTABLE_MESSAGE_THRESHOLD = 50
DEFAULT_USER_TIMEOUT = timedelta(hours=24)
MESSAGE_ALERT_DELETE_AFTER = timedelta(minutes=5)
THREAD_ALERT_DELETE_AFTER = timedelta(minutes=60)
MAX_BULK_REPORT_UPDATES = 5

MODERATOR_ROLES = set(cfg.rocketpool.support.moderator_roles)
ADMIN_ROLES = set(cfg.rocketpool.support.admin_roles)


class AutomodAction(Enum):
    MESSAGE_DELETED = "message_deleted"
    THREAD_LOCKED = "thread_locked"
    MEMBER_TIMED_OUT = "member_timed_out"


@dataclass
class ReportContext:
    bot: RocketWatch
    sentinel: SentinelClient


class ReportColor:
    ALERT = CustomColors.RED
    WARN = CustomColors.YELLOW
    OK = CustomColors.GREEN


class PartnerBroadcast(TypedDict):
    guild_id: int
    channel_id: int
    message_id: int


class ScamReport(TypedDict):
    type: str
    guild_id: int | None
    user_id: int
    reason: str
    content: str | None
    warning_id: int | None
    report_id: int
    user_banned: bool
    channel_id: int
    message_id: int
    embeds: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    forwarded_from: list[dict[str, Any]]
    message_deleted: bool
    thread_removed: bool
    partner_messages: list[PartnerBroadcast]


def is_reputable(member: Member) -> bool:
    return (
        is_admin(member)
        or member.guild_permissions.moderate_members
        or member.id == cfg.discord.owner.user_id
        or member.id in cfg.rocketpool.support.user_ids
        or bool({role.id for role in member.roles} & MODERATOR_ROLES)
    )


def is_admin(member: Member) -> bool:
    return member.guild_permissions.ban_members or bool(
        {role.id for role in member.roles} & ADMIN_ROLES
    )


async def get_report_channel(ctx: ReportContext) -> Messageable:
    channel = await ctx.bot.get_or_fetch_channel(cfg.discord.channels["report_scams"])
    assert isinstance(channel, Messageable)
    return channel


def _attachment_summary(attachment: Any) -> dict[str, Any]:
    return {
        "filename": attachment.filename,
        "url": attachment.url,
        "content_type": attachment.content_type,
        "size": attachment.size,
    }


def message_to_dict(message: Message) -> dict[str, Any]:
    """Serialize a Discord message into a dict that captures content, embeds,
    attachments, and any forwarded snapshots — for storage and LLM inspection.
    """
    data: dict[str, Any] = {"content": message.content}
    if message.embeds:
        data["embeds"] = [
            {"title": e.title, "description": e.description} for e in message.embeds
        ]
    if message.attachments:
        data["attachments"] = [_attachment_summary(a) for a in message.attachments]
    snapshots = getattr(message, "message_snapshots", []) or []
    if snapshots:
        data["forwarded_from"] = [
            {
                "content": s.content,
                "embeds": [
                    {"title": e.title, "description": e.description} for e in s.embeds
                ],
                "attachments": [_attachment_summary(a) for a in s.attachments],
            }
            for s in snapshots
        ]
    return data


def flatten_forwarded_message(message: Message) -> None:
    """Merge forwarded message snapshots into the message's own fields in place,
    so downstream checks see the forwarded content as if it were sent directly.
    """
    snapshots = getattr(message, "message_snapshots", [])
    if not snapshots:
        return
    parts = [message.content] if message.content else []
    parts.extend(s.content for s in snapshots if s.content)
    message.content = "\n\n".join(parts)
    message.embeds = [*message.embeds, *(e for s in snapshots for e in s.embeds)]
    message.attachments = [
        *message.attachments,
        *(a for s in snapshots for a in s.attachments),
    ]


async def member_from_message(bot: RocketWatch, message: Message) -> Member | None:
    if isinstance(message.author, Member):
        return message.author
    if not message.guild:
        return None
    return await bot.get_or_fetch_member(message.guild.id, message.author.id)


async def member_from_interaction(
    interaction: Interaction[RocketWatch],
) -> Member | None:
    if isinstance(interaction.user, Member):
        return interaction.user
    if not interaction.guild:
        return None
    return await interaction.client.get_or_fetch_member(
        interaction.guild.id, interaction.user.id
    )


def build_automod_embed(report_msg: Message, actions: list[str]) -> Embed:
    description = ""
    if len(actions) > 1:
        description = ", ".join(actions[:-1]) + " and "
    description += actions[-1] + "."

    if not description.startswith("http"):
        # capitalize first letter unless URL
        description = description[0].upper() + description[1:]

    return Embed(
        title=":hammer: Automated Moderation",
        url=report_msg.jump_url,
        color=ReportColor.ALERT,
        description=description,
    )


async def update_report(ctx: ReportContext, report_msg_id: int, note: str) -> None:
    try:
        report_channel = await get_report_channel(ctx)
        message = await report_channel.fetch_message(report_msg_id)
        if (not message.embeds) or (message.embeds[0].color == ReportColor.OK):
            return

        embed = message.embeds[0]
        embed.description = (embed.description or "") + f"\n\n**{note}**"
        embed.color = ReportColor.WARN
        await message.edit(embed=embed)
    except Exception as e:
        await ctx.bot.report_error(e)


async def resolve_report(ctx: ReportContext, report_msg_id: int, note: str) -> None:
    try:
        report_channel = await get_report_channel(ctx)
        message = await report_channel.fetch_message(report_msg_id)
        if not message.embeds:
            return
        embed = message.embeds[0]
        embed.description = (embed.description or "") + f"\n\n**{note}**"
        embed.color = ReportColor.OK
        await message.edit(embed=embed, view=None)
    except Exception as e:
        await ctx.bot.report_error(e)
