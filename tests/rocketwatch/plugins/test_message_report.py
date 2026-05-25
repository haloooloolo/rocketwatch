import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.abc import Messageable
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.scam_detection import message_report as mr
from rocketwatch.plugins.scam_detection.common import AutomodAction
from rocketwatch.plugins.scam_detection.views import ReportReviewView
from tests.lib.discord_harness import make_bot
from tests.lib.scam_detection_harness import ScriptedSentinel, make_ctx


def _make_message(**over: Any) -> MagicMock:
    m = MagicMock()
    m.id = over.get("id", 100)
    m.content = over.get("content", "hello")
    m.embeds = over.get("embeds", [])
    m.attachments = over.get("attachments", [])
    m.message_snapshots = over.get("snapshots", [])
    m.author.id = over.get("author_id", 1)
    m.author.mention = "<@1>"
    m.jump_url = "http://msg"
    m.channel.id = over.get("channel_id", 7)
    m.channel.jump_url = "http://chan"
    m.guild.id = over.get("guild_id", 9)
    return m


class TestGenerateEmbeds:
    def test_builds_warning_report_and_attachment(self) -> None:
        warning, report, attachment = mr._generate_embeds(_make_message(), "phishing")
        assert warning.title is not None and "Likely Scam" in warning.title
        assert report.description is not None
        assert "phishing" in report.description
        assert "<@1>" in report.description
        assert attachment.filename == "message.json"


class TestSerializeMessage:
    def test_serializes_content_to_json(self) -> None:
        out = mr.serialize_message(_make_message(content="gm frens"))
        assert json.loads(out)["content"] == "gm frens"


class TestBuildReviewView:
    def test_returns_view_when_sentinel_enabled(self) -> None:
        ctx = make_ctx(sentinel=ScriptedSentinel(enabled=True))
        assert isinstance(mr._build_review_view(ctx), ReportReviewView)

    def test_returns_none_when_sentinel_disabled(self) -> None:
        ctx = make_ctx(sentinel=ScriptedSentinel(enabled=False))
        assert mr._build_review_view(ctx) is None


class TestClaimMessageReport:
    async def test_claim_is_idempotent(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        ctx = make_ctx(db=mongo_db)
        assert await mr._claim_message_report(ctx, 100) is True
        # Second claim of the same message finds the placeholder → not claimed.
        assert await mr._claim_message_report(ctx, 100) is False
        # Releasing frees the slot again.
        await mr._release_claim(ctx, 100)
        assert await mr._claim_message_report(ctx, 100) is True


class TestFinalizeReport:
    async def test_replaces_placeholder_with_full_doc(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        ctx = make_ctx(db=mongo_db)
        await mr._claim_message_report(ctx, 100)
        report_msg = MagicMock(id=200)
        await mr._finalize_report(ctx, _make_message(), "phishing", None, report_msg)

        doc = await mongo_db.scam_reports.find_one({"message_id": 100})
        assert doc is not None
        assert doc["type"] == "message"
        assert doc["user_id"] == 1
        assert doc["reason"] == "phishing"
        assert doc["report_id"] == 200
        assert doc["message_deleted"] is False


class TestOnMessageDelete:
    async def test_marks_deleted_and_removes_warning(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.scam_reports.insert_one(
            {
                "type": "message",
                "message_id": 100,
                "channel_id": 7,
                "warning_id": 55,
                "report_id": 200,
                "message_deleted": False,
            }
        )
        fetched = MagicMock()
        fetched.delete = AsyncMock()
        fetched.embeds = []  # makes update_report return early
        channel = MagicMock(spec=Messageable)
        channel.fetch_message = AsyncMock(return_value=fetched)
        bot = make_bot(db=mongo_db)
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        ctx = make_ctx(bot=bot)

        await mr.on_message_delete(ctx, 100)

        doc = await mongo_db.scam_reports.find_one({"message_id": 100})
        assert doc is not None and doc["message_deleted"] is True
        fetched.delete.assert_awaited()

    async def test_unknown_message_is_noop(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        bot = make_bot(db=mongo_db)
        bot.get_or_fetch_channel = AsyncMock()
        ctx = make_ctx(bot=bot)
        await mr.on_message_delete(ctx, 999)
        bot.get_or_fetch_channel.assert_not_awaited()


class TestRunMessageAutomod:
    async def test_aggregates_delete_and_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = make_ctx()
        member = MagicMock()
        member.mention = "<@5>"
        monkeypatch.setattr(mr, "member_from_message", AsyncMock(return_value=member))

        channel = MagicMock(spec=Messageable)
        channel.send = AsyncMock()
        message = _make_message()
        message.channel = channel
        report_msg = MagicMock(jump_url="http://r")

        actions = await mr.run_message_automod(ctx, message, "scam", report_msg)

        assert AutomodAction.MESSAGE_DELETED in actions
        assert AutomodAction.MEMBER_TIMED_OUT in actions
        # Non-thread channel → no lock.
        assert AutomodAction.THREAD_LOCKED not in actions
        channel.send.assert_awaited()

    async def test_no_member_skips_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = make_ctx()
        monkeypatch.setattr(mr, "member_from_message", AsyncMock(return_value=None))
        channel = MagicMock(spec=Messageable)
        channel.send = AsyncMock()
        message = _make_message()
        message.channel = channel

        actions = await mr.run_message_automod(
            ctx, message, "scam", MagicMock(jump_url="http://r")
        )
        assert actions == {AutomodAction.MESSAGE_DELETED}

    async def test_exception_is_reported_and_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bot = make_bot()
        sentinel = ScriptedSentinel()
        sentinel.delete_message = AsyncMock(side_effect=RuntimeError("boom"))
        ctx = make_ctx(bot=bot, sentinel=sentinel)
        monkeypatch.setattr(mr, "member_from_message", AsyncMock(return_value=None))
        message = _make_message()

        actions = await mr.run_message_automod(
            ctx, message, "scam", MagicMock(jump_url="http://r")
        )
        assert actions == set()
        bot.report_error.assert_awaited()
