from typing import Any
from unittest.mock import AsyncMock, MagicMock

from discord import Thread
from discord.abc import Messageable
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.scam_detection import thread_report as tr
from rocketwatch.plugins.scam_detection.common import AutomodAction
from tests.lib.discord_harness import make_bot
from tests.lib.scam_detection_harness import make_ctx


def _make_thread(**over: Any) -> MagicMock:
    t = MagicMock(spec=Thread)
    t.id = over.get("id", 300)
    t.name = over.get("name", "sketchy thread")
    t.owner_id = over.get("owner_id", 11)
    t.guild.id = over.get("guild_id", 9)
    t.jump_url = "http://thread"
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
