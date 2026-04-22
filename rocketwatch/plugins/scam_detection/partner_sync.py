import asyncio
import logging

from discord import AllowedMentions, Forbidden, Message, NotFound
from discord.abc import Messageable

from rocketwatch.plugins.scam_detection.common import PartnerBroadcast, ReportContext
from rocketwatch.utils.config import PartnerGuild, cfg

log = logging.getLogger("rocketwatch.scam_detection")


async def _broadcast_to_partner(
    ctx: ReportContext, partner: PartnerGuild, user_id: int, report_msg: Message
) -> PartnerBroadcast | None:
    try:
        member = await ctx.bot.get_or_fetch_member(partner.guild_id, user_id)
    except (NotFound, Forbidden):
        return None
    except Exception as e:
        log.warning(
            f"Failed to look up user {user_id} in partner guild "
            f"{partner.guild_id}: {e!r}"
        )
        return None

    try:
        channel = await ctx.bot.get_or_fetch_channel(partner.report_channel_id)
    except Exception as e:
        log.warning(
            f"Failed to fetch report channel {partner.report_channel_id} "
            f"in partner guild {partner.guild_id}: {e!r}"
        )
        return None

    if not isinstance(channel, Messageable):
        log.warning(
            f"Partner guild {partner.guild_id} report channel "
            f"{partner.report_channel_id} is not messageable"
        )
        return None

    sent = await channel.send(
        f"Flagged {member.mention} — report: {report_msg.jump_url}",
        allowed_mentions=AllowedMentions.none(),
    )
    return PartnerBroadcast(
        guild_id=partner.guild_id,
        channel_id=partner.report_channel_id,
        message_id=sent.id,
    )


async def broadcast_user_report(
    ctx: ReportContext, user_id: int, report_msg: Message
) -> None:
    partners = cfg.scam_detection.partners
    if not partners:
        return

    results = await asyncio.gather(
        *[_broadcast_to_partner(ctx, p, user_id, report_msg) for p in partners],
        return_exceptions=True,
    )

    broadcasts: list[PartnerBroadcast] = []
    for partner, result in zip(partners, results, strict=True):
        if isinstance(result, BaseException):
            log.error(
                f"Partner broadcast to guild {partner.guild_id} failed",
                exc_info=result,
            )
            if isinstance(result, Exception):
                await ctx.bot.report_error(result)
        elif result is not None:
            broadcasts.append(result)

    if not broadcasts:
        return

    await ctx.bot.db.scam_reports.update_one(
        {"report_id": report_msg.id},
        {"$set": {"partner_messages": broadcasts}},
    )
