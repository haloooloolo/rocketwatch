import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Any, NotRequired, TypedDict

from discord import Color, Member, Message
from discord.abc import Messageable

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.sentinel import SentinelClient

log = logging.getLogger("rocketwatch.scam_detection")

REPUTABLE_MESSAGE_THRESHOLD = 50
DEFAULT_USER_TIMEOUT = timedelta(hours=24)
MESSAGE_ALERT_DELETE_AFTER = timedelta(minutes=5)
THREAD_ALERT_DELETE_AFTER = timedelta(minutes=60)


class AutomodAction(Enum):
    MESSAGE_DELETED = "message_deleted"
    THREAD_LOCKED = "thread_locked"
    MEMBER_TIMED_OUT = "member_timed_out"


@dataclass
class ReportContext:
    bot: RocketWatch
    sentinel: SentinelClient


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
    message_deleted: NotRequired[bool]
    thread_removed: NotRequired[bool]


def is_reputable(user: Member) -> bool:
    return any(
        (
            user.id == cfg.discord.owner.user_id,
            user.id in cfg.rocketpool.support.user_ids,
            {role.id for role in user.roles} & set(cfg.rocketpool.support.role_ids),
            user.guild_permissions.moderate_members,
        )
    )


async def get_report_channel(ctx: ReportContext) -> Messageable:
    channel = await ctx.bot.get_or_fetch_channel(cfg.discord.channels["report_scams"])
    assert isinstance(channel, Messageable)
    return channel


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
