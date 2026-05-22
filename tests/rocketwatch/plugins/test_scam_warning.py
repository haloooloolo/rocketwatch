from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord import Member, errors
from discord.abc import Messageable
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.scam_warning import scam_warning as sw_module
from rocketwatch.plugins.scam_warning.scam_warning import ScamWarning
from rocketwatch.utils.config import cfg
from tests.lib.discord_harness import make_bot, make_interaction


@pytest.fixture
def _resources_channel(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # _build_warning_embed reads cfg.discord.channels["resources"].
    monkeypatch.setitem(cfg.discord.channels, "resources", 5)
    yield


def _messageable(mention: str) -> MagicMock:
    ch = MagicMock(spec=Messageable)
    ch.mention = mention
    return ch


class TestBuildWarningEmbed:
    async def test_includes_scam_count_when_nonzero(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        _resources_channel: None,
    ) -> None:
        # Two recent scam reports → count surfaces in the description.
        await mongo_db.scam_reports.insert_many([{"x": 1}, {"x": 2}])
        bot = make_bot(db=mongo_db)
        bot.get_or_fetch_channel = AsyncMock(
            side_effect=[_messageable("#support"), _messageable("#resources")]
        )
        cog = ScamWarning(bot)
        embed = await cog._build_warning_embed()
        assert embed.description is not None
        assert "2 scam attempts" in embed.description
        assert "#support" in embed.description
        assert "#resources" in embed.description

    async def test_omits_count_when_zero(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        _resources_channel: None,
    ) -> None:
        bot = make_bot(db=mongo_db)
        bot.get_or_fetch_channel = AsyncMock(
            side_effect=[_messageable("#support"), _messageable("#resources")]
        )
        cog = ScamWarning(bot)
        embed = await cog._build_warning_embed()
        assert embed.description is not None
        assert "scam attempts" not in embed.description


class TestPreviewCommand:
    async def test_sends_embed(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        _resources_channel: None,
    ) -> None:
        bot = make_bot(db=mongo_db)
        bot.get_or_fetch_channel = AsyncMock(
            side_effect=[_messageable("#support"), _messageable("#resources")]
        )
        cog = ScamWarning(bot)
        interaction = make_interaction()
        await cog.preview_scam_warning.callback(cog, interaction)
        interaction.response.send_message.assert_awaited_once()


def _message(*, channel_id: int, author: Any, created_at: datetime) -> MagicMock:
    msg = MagicMock()
    msg.channel.id = channel_id
    msg.author = author
    msg.created_at = created_at
    return msg


class TestOnMessage:
    @pytest.fixture(autouse=True)
    def _stub_embed(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        # _build_warning_embed hits channels/db; the on_message logic doesn't
        # care about its content, only that send_warning runs.
        from rocketwatch.utils.embeds import Embed

        monkeypatch.setattr(
            ScamWarning, "_build_warning_embed", AsyncMock(return_value=Embed())
        )
        yield

    async def test_ignores_message_outside_target_channels(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        bot = make_bot(db=mongo_db)
        cog = ScamWarning(bot)
        cog.channel_ids = {123}
        author = MagicMock()
        author.id = 7
        await cog.on_message(
            _message(channel_id=999, author=author, created_at=datetime.now(UTC))
        )
        assert await mongo_db.scam_warning.count_documents({}) == 0

    async def test_ignores_bot_own_message(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        bot = make_bot(db=mongo_db)
        cog = ScamWarning(bot)
        cog.channel_ids = {123}
        bot.user = MagicMock()
        await cog.on_message(
            _message(channel_id=123, author=bot.user, created_at=datetime.now(UTC))
        )
        assert await mongo_db.scam_warning.count_documents({}) == 0

    async def test_skips_reputable_member(
        self, mongo_db: AsyncDatabase[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sw_module, "is_reputable", lambda _m: True)
        bot = make_bot(db=mongo_db)
        cog = ScamWarning(bot)
        cog.channel_ids = {123}
        author = MagicMock(spec=Member)
        author.id = 7
        await cog.on_message(
            _message(channel_id=123, author=author, created_at=datetime.now(UTC))
        )
        assert await mongo_db.scam_warning.count_documents({}) == 0

    async def test_first_message_sends_dm_and_records_success(
        self, mongo_db: AsyncDatabase[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sw_module, "is_reputable", lambda _m: False)
        bot = make_bot(db=mongo_db)
        cog = ScamWarning(bot)
        cog.channel_ids = {123}
        author = MagicMock(spec=Member)
        author.id = 7
        author.send = AsyncMock()
        await cog.on_message(
            _message(channel_id=123, author=author, created_at=datetime.now(UTC))
        )
        author.send.assert_awaited_once()
        doc = await mongo_db.scam_warning.find_one({"_id": 7})
        assert doc is not None
        assert doc["last_success"] is not None
        assert doc["last_failure"] is None

    async def test_dm_failure_records_failure(
        self, mongo_db: AsyncDatabase[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sw_module, "is_reputable", lambda _m: False)
        bot = make_bot(db=mongo_db)
        cog = ScamWarning(bot)
        cog.channel_ids = {123}
        author = MagicMock(spec=Member)
        author.id = 7
        author.send = AsyncMock(
            side_effect=errors.Forbidden(MagicMock(status=403), "blocked")
        )
        await cog.on_message(
            _message(channel_id=123, author=author, created_at=datetime.now(UTC))
        )
        doc = await mongo_db.scam_warning.find_one({"_id": 7})
        assert doc is not None
        assert doc["last_failure"] is not None
        assert doc["last_success"] is None

    async def test_within_cooldown_does_not_resend(
        self, mongo_db: AsyncDatabase[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Round-trips a stored datetime: the tz-aware fixture hands it back
        # aware, so this exercises the plugin's naive-normalisation path.
        monkeypatch.setattr(sw_module, "is_reputable", lambda _m: False)
        now = datetime.now(UTC)
        # A recent successful message → still inside the 90-day inactivity window.
        await mongo_db.scam_warning.insert_one(
            {
                "_id": 7,
                "last_message": (now - timedelta(days=1)).replace(tzinfo=None),
                "last_success": (now - timedelta(days=1)).replace(tzinfo=None),
                "last_failure": None,
            }
        )
        bot = make_bot(db=mongo_db)
        cog = ScamWarning(bot)
        cog.channel_ids = {123}
        author = MagicMock(spec=Member)
        author.id = 7
        author.send = AsyncMock()
        await cog.on_message(_message(channel_id=123, author=author, created_at=now))
        author.send.assert_not_awaited()
        doc = await mongo_db.scam_warning.find_one({"_id": 7})
        assert doc is not None
        assert doc["last_message"] is not None
