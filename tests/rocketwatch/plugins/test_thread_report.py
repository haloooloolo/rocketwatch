from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord import Thread
from discord.abc import Messageable
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.scam_detection import thread_report as tr
from rocketwatch.plugins.scam_detection.common import AutomodAction
from rocketwatch.plugins.scam_detection.views import ReportReviewView
from tests.lib.discord_harness import make_bot
from tests.lib.scam_detection_harness import ScriptedSentinel, make_ctx

Db = AsyncDatabase[dict[str, Any]]


def _make_thread(**over: Any) -> MagicMock:
    t = MagicMock(spec=Thread)
    t.id = over.get("id", 300)
    t.name = over.get("name", "sketchy thread")
    t.owner_id = over.get("owner_id", 11)
    t.guild.id = over.get("guild_id", 9)
    t.jump_url = "http://thread"
    t.created_at = datetime(2024, 1, 1, tzinfo=UTC)
    t.message_count = 3
    t.member_count = 2
    t.send = AsyncMock(return_value=MagicMock(id=500))
    parent = MagicMock(spec=Messageable)
    parent.send = AsyncMock()
    t.parent = parent
    return t


class TestGenerateEmbeds:
    def test_builds_warning_and_report(self) -> None:
        warning, report = tr._generate_embeds(_make_thread(), "luring to DMs")
        assert warning.title is not None and "Likely Scam" in warning.title
        assert report.description is not None and "luring to DMs" in report.description


class TestClaimThreadReport:
    async def test_claim_is_idempotent(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        ctx = make_ctx(db=mongo_db)
        assert await tr._claim_thread_report(ctx, 300) is True
        assert await tr._claim_thread_report(ctx, 300) is False
        await tr._release_claim(ctx, 300)
        assert await tr._claim_thread_report(ctx, 300) is True


class TestFinalizeReport:
    async def test_writes_full_doc(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        ctx = make_ctx(db=mongo_db)
        await tr._claim_thread_report(ctx, 300)
        await tr._finalize_report(
            ctx, _make_thread(), "luring", None, MagicMock(id=200)
        )
        doc = await mongo_db.scam_reports.find_one({"channel_id": 300})
        assert doc is not None
        assert doc["type"] == "thread"
        assert doc["user_id"] == 11
        assert doc["report_id"] == 200
        assert doc["thread_removed"] is False


class TestRunThreadAutomod:
    async def test_aggregates_lock_and_timeout(self) -> None:
        member = MagicMock()
        member.mention = "<@11>"
        bot = make_bot()
        bot.get_or_fetch_member = AsyncMock(return_value=member)
        ctx = make_ctx(bot=bot)
        thread = _make_thread()
        report_msg = MagicMock(jump_url="http://r")

        actions = await tr.run_thread_automod(ctx, thread, "scam", report_msg)

        assert AutomodAction.THREAD_LOCKED in actions
        assert AutomodAction.MEMBER_TIMED_OUT in actions
        thread.parent.send.assert_awaited()

    async def test_exception_is_reported(self) -> None:
        bot = make_bot()
        bot.get_or_fetch_member = AsyncMock(side_effect=RuntimeError("boom"))
        ctx = make_ctx(bot=bot)
        actions = await tr.run_thread_automod(
            ctx, _make_thread(), "scam", MagicMock(jump_url="http://r")
        )
        assert actions == set()
        bot.report_error.assert_awaited()


class TestOnThreadRemoved:
    async def test_marks_removed_and_clears_warning(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.scam_reports.insert_one(
            {
                "type": "thread",
                "channel_id": 300,
                "warning_id": 55,
                "report_id": 200,
                "thread_removed": False,
            }
        )
        channel = MagicMock(spec=Messageable)
        fetched = MagicMock()
        fetched.embeds = []  # update_report returns early
        channel.fetch_message = AsyncMock(return_value=fetched)
        bot = make_bot(db=mongo_db)
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        ctx = make_ctx(bot=bot)

        await tr.on_thread_removed(ctx, 300, "Thread deleted.")

        doc = await mongo_db.scam_reports.find_one({"channel_id": 300})
        assert doc is not None
        assert doc["thread_removed"] is True
        assert doc["warning_id"] is None

    async def test_non_thread_report_keeps_warning(
        self, mongo_db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A message report sharing the channel_id: thread_removed flips, but the
        # warning_id clearing (thread-only) is skipped.
        await mongo_db.scam_reports.insert_one(
            {
                "type": "message",
                "channel_id": 300,
                "warning_id": 55,
                "report_id": 200,
                "thread_removed": False,
            }
        )
        update = AsyncMock()
        monkeypatch.setattr(tr, "update_report", update)
        ctx = make_ctx(bot=make_bot(db=mongo_db))

        await tr.on_thread_removed(ctx, 300, "Thread deleted.")

        doc = await mongo_db.scam_reports.find_one({"channel_id": 300})
        assert doc is not None
        assert doc["thread_removed"] is True
        assert doc["warning_id"] == 55
        update.assert_awaited()


class TestBuildReviewView:
    def test_returns_view_when_enabled(self) -> None:
        ctx = make_ctx(sentinel=ScriptedSentinel(enabled=True))
        assert isinstance(tr._build_review_view(ctx), ReportReviewView)

    def test_returns_none_when_disabled(self) -> None:
        ctx = make_ctx(sentinel=ScriptedSentinel(enabled=False))
        assert tr._build_review_view(ctx) is None


class TestRunThreadAutomodBranches:
    async def test_no_actions_skips_alert(self) -> None:
        sentinel = ScriptedSentinel()
        sentinel.timeout_member = AsyncMock(return_value=False)
        sentinel.lock_thread = AsyncMock(return_value=False)
        bot = make_bot()
        bot.get_or_fetch_member = AsyncMock(return_value=MagicMock())
        ctx = make_ctx(bot=bot, sentinel=sentinel)
        thread = _make_thread()

        actions = await tr.run_thread_automod(
            ctx, thread, "scam", MagicMock(jump_url="http://r")
        )

        assert actions == set()
        thread.parent.send.assert_not_awaited()


def _report_channel(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    channel = MagicMock(spec=Messageable)
    channel.send = AsyncMock(return_value=MagicMock(id=200, jump_url="http://r"))
    monkeypatch.setattr(tr, "get_report_channel", AsyncMock(return_value=channel))
    return channel


class TestReportThread:
    def _ctx(self, mongo_db: Db) -> Any:
        bot = make_bot(db=mongo_db)
        bot.get_or_fetch_user = AsyncMock(return_value=MagicMock(mention="<@11>"))
        return make_ctx(bot=bot)

    async def test_creates_report_and_sends_warning(
        self, mongo_db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = self._ctx(mongo_db)
        _report_channel(monkeypatch)
        monkeypatch.setattr(tr, "run_thread_automod", AsyncMock(return_value=set()))
        thread = _make_thread()

        await tr.report_thread(ctx, thread, "luring to DMs")

        thread.send.assert_awaited_once()
        doc = await mongo_db.scam_reports.find_one({"channel_id": 300})
        assert doc is not None
        assert doc["report_id"] == 200
        assert doc["warning_id"] == 500

    async def test_duplicate_returns_early(
        self, mongo_db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await mongo_db.scam_reports.insert_one({"type": "thread", "channel_id": 300})
        ctx = self._ctx(mongo_db)
        channel = _report_channel(monkeypatch)
        await tr.report_thread(ctx, _make_thread(), "scam")
        channel.send.assert_not_awaited()

    async def test_warning_send_failure_is_tolerated(
        self, mongo_db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from discord import errors

        ctx = self._ctx(mongo_db)
        _report_channel(monkeypatch)
        monkeypatch.setattr(tr, "run_thread_automod", AsyncMock(return_value=set()))
        thread = _make_thread()
        thread.send = AsyncMock(side_effect=errors.Forbidden(MagicMock(), "x"))

        await tr.report_thread(ctx, thread, "scam")

        doc = await mongo_db.scam_reports.find_one({"channel_id": 300})
        # report still finalized; warning_id stays None since the warning failed
        assert doc is not None and doc["warning_id"] is None

    async def test_failure_releases_claim_and_raises(
        self, mongo_db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = self._ctx(mongo_db)
        monkeypatch.setattr(
            tr, "get_report_channel", AsyncMock(side_effect=RuntimeError("boom"))
        )

        with pytest.raises(RuntimeError):
            await tr.report_thread(ctx, _make_thread(), "scam")

        # the claimed placeholder is rolled back
        assert await mongo_db.scam_reports.find_one({"channel_id": 300}) is None


class TestCheckThreadStarterDeleted:
    async def test_reports_when_owner_not_banned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        thread = _make_thread()
        bot = make_bot()
        bot.get_or_fetch_channel = AsyncMock(return_value=thread)
        ctx = make_ctx(bot=bot)
        report = AsyncMock()
        monkeypatch.setattr(tr, "report_thread", report)

        await tr.check_thread_starter_deleted(ctx, 42, {42: 300})

        report.assert_awaited_once()

    async def test_skips_when_owner_banned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        thread = _make_thread()
        sentinel = ScriptedSentinel()
        sentinel.is_banned = AsyncMock(return_value=True)
        bot = make_bot()
        bot.get_or_fetch_channel = AsyncMock(return_value=thread)
        ctx = make_ctx(bot=bot, sentinel=sentinel)
        report = AsyncMock()
        monkeypatch.setattr(tr, "report_thread", report)

        await tr.check_thread_starter_deleted(ctx, 42, {42: 300})

        report.assert_not_awaited()

    async def test_unknown_message_is_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bot = make_bot()
        ctx = make_ctx(bot=bot)
        report = AsyncMock()
        monkeypatch.setattr(tr, "report_thread", report)

        await tr.check_thread_starter_deleted(ctx, 999, {42: 300})

        report.assert_not_awaited()
        bot.get_or_fetch_channel.assert_not_called()

    async def test_non_thread_channel_is_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bot = make_bot()
        bot.get_or_fetch_channel = AsyncMock(return_value=MagicMock())  # not a Thread
        ctx = make_ctx(bot=bot)
        report = AsyncMock()
        monkeypatch.setattr(tr, "report_thread", report)

        await tr.check_thread_starter_deleted(ctx, 42, {42: 300})

        report.assert_not_awaited()
