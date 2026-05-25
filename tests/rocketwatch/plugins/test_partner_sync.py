from unittest.mock import AsyncMock, MagicMock

from discord.abc import Messageable

from rocketwatch.plugins.scam_detection import partner_sync as ps
from rocketwatch.utils.config import PartnerGuild
from tests.lib.discord_harness import make_bot
from tests.lib.scam_detection_harness import make_ctx


def _partner(guild_id: int = 1, channel_id: int = 2) -> PartnerGuild:
    return PartnerGuild(guild_id=guild_id, report_channel_id=channel_id)


class TestBroadcastToPartner:
    async def test_sends_and_returns_broadcast(self) -> None:
        bot = make_bot()
        bot.get_or_fetch_member = AsyncMock(return_value=MagicMock())
        channel = MagicMock(spec=Messageable)
        channel.send = AsyncMock(return_value=MagicMock(id=300))
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)

        result = await ps._broadcast_to_partner(
            make_ctx(bot=bot), _partner(1, 2), 99, lambda _m: "flagged"
        )
        assert result == {"guild_id": 1, "channel_id": 2, "message_id": 300}

    async def test_member_lookup_failure_returns_none(self) -> None:
        bot = make_bot()
        bot.get_or_fetch_member = AsyncMock(side_effect=RuntimeError("nope"))
        result = await ps._broadcast_to_partner(
            make_ctx(bot=bot), _partner(), 99, lambda _m: "flagged"
        )
        assert result is None

    async def test_non_messageable_channel_returns_none(self) -> None:
        bot = make_bot()
        bot.get_or_fetch_member = AsyncMock(return_value=MagicMock())
        bot.get_or_fetch_channel = AsyncMock(return_value=object())
        result = await ps._broadcast_to_partner(
            make_ctx(bot=bot), _partner(), 99, lambda _m: "flagged"
        )
        assert result is None


class TestBroadcast:
    async def test_collects_successes_and_reports_failures(self) -> None:
        bot = make_bot()
        bot.get_or_fetch_member = AsyncMock(return_value=MagicMock())

        good_channel = MagicMock(spec=Messageable)
        good_channel.send = AsyncMock(return_value=MagicMock(id=300))
        bad_channel = MagicMock(spec=Messageable)
        bad_channel.send = AsyncMock(side_effect=RuntimeError("send failed"))
        bot.get_or_fetch_channel = AsyncMock(side_effect=[good_channel, bad_channel])

        broadcasts = await ps._broadcast(
            make_ctx(bot=bot),
            [_partner(1, 2), _partner(3, 4)],
            99,
            lambda _m: "flagged",
        )
        assert broadcasts == [{"guild_id": 1, "channel_id": 2, "message_id": 300}]
        bot.report_error.assert_awaited()


class TestBroadcastUserReport:
    async def test_no_partners_is_noop(self) -> None:
        # Baseline cfg has no partners configured → returns without touching db.
        ctx = make_ctx()
        await ps.broadcast_user_report(ctx, 99, MagicMock(id=200))
