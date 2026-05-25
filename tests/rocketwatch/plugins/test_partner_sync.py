from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord import Guild
from discord.abc import Messageable
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.scam_detection import partner_sync as ps
from rocketwatch.utils.config import PartnerGuild, cfg
from tests.lib.cfg import make_cfg
from tests.lib.discord_harness import make_bot
from tests.lib.scam_detection_harness import make_ctx

Db = AsyncDatabase[dict[str, Any]]


def _partner(guild_id: int = 1, channel_id: int = 2) -> PartnerGuild:
    return PartnerGuild(guild_id=guild_id, report_channel_id=channel_id)


def _with_partners(
    monkeypatch: pytest.MonkeyPatch, partners: list[PartnerGuild]
) -> None:
    c = make_cfg()
    c.scam_detection.partners = partners
    monkeypatch.setattr(cfg, "_instance", c)


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

    async def test_member_not_found_returns_none(self) -> None:
        from discord import NotFound

        bot = make_bot()
        bot.get_or_fetch_member = AsyncMock(side_effect=NotFound(MagicMock(), "x"))
        result = await ps._broadcast_to_partner(
            make_ctx(bot=bot), _partner(), 99, lambda _m: "flagged"
        )
        assert result is None

    async def test_channel_fetch_failure_returns_none(self) -> None:
        bot = make_bot()
        bot.get_or_fetch_member = AsyncMock(return_value=MagicMock())
        bot.get_or_fetch_channel = AsyncMock(side_effect=RuntimeError("boom"))
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

    async def test_records_broadcasts_on_report(
        self, mongo_db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_partners(monkeypatch, [_partner(1, 2)])
        bot = make_bot(db=mongo_db)
        bot.get_or_fetch_member = AsyncMock(return_value=MagicMock(mention="<@9>"))
        channel = MagicMock(spec=Messageable)
        channel.send = AsyncMock(return_value=MagicMock(id=300))
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        ctx = make_ctx(bot=bot)
        await mongo_db.scam_reports.insert_one({"report_id": 200})

        await ps.broadcast_user_report(ctx, 99, MagicMock(id=200, jump_url="http://r"))

        doc = await mongo_db.scam_reports.find_one({"report_id": 200})
        assert doc is not None
        assert doc["partner_messages"] == [
            {"guild_id": 1, "channel_id": 2, "message_id": 300}
        ]

    async def test_no_successful_broadcasts_skips_db_write(
        self, mongo_db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_partners(monkeypatch, [_partner(1, 2)])
        bot = make_bot(db=mongo_db)
        # member lookup returns None → broadcast yields nothing
        bot.get_or_fetch_member = AsyncMock(return_value=None)
        ctx = make_ctx(bot=bot)
        await mongo_db.scam_reports.insert_one({"report_id": 200})

        await ps.broadcast_user_report(ctx, 99, MagicMock(id=200, jump_url="http://r"))

        doc = await mongo_db.scam_reports.find_one({"report_id": 200})
        assert doc is not None
        assert "partner_messages" not in doc


class TestBroadcastPartnerBan:
    async def test_excludes_source_guild(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _with_partners(monkeypatch, [_partner(1, 2), _partner(5, 6)])
        bot = make_bot()
        bot.get_or_fetch_member = AsyncMock(return_value=MagicMock(mention="<@9>"))
        channel = MagicMock(spec=Messageable)
        channel.send = AsyncMock(return_value=MagicMock(id=300))
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        ctx = make_ctx(bot=bot)
        source = MagicMock(spec=Guild)
        source.id = 5
        source.name = "Source"

        await ps.broadcast_partner_ban(ctx, source, 99)

        # only the non-source partner (guild 1) is messaged
        channel.send.assert_awaited_once()

    async def test_only_source_partner_is_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_partners(monkeypatch, [_partner(5, 6)])
        bot = make_bot()
        ctx = make_ctx(bot=bot)
        source = MagicMock(spec=Guild)
        source.id = 5
        source.name = "Source"

        await ps.broadcast_partner_ban(ctx, source, 99)

        bot.get_or_fetch_member.assert_not_called()
