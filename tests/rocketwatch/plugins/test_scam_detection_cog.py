from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord import MessageType, Thread
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.scam_detection import scam_detection as sd
from rocketwatch.plugins.scam_detection.scam_detection import ScamDetection
from tests.lib.discord_harness import make_bot

SERVER_ID = 1  # matches the baseline cfg's rocketpool.support.server_id


def _make_cog(bot: Any, *, checks: Any = None, llm: Any = None) -> ScamDetection:
    # bot.tree.add_command is a MagicMock on the fake bot, so __init__ runs fine.
    # checks/llm are passed/held as Any so tests configure & assert on the mock,
    # not through the cog's typed attributes.
    cog = ScamDetection(bot)
    cog._checks = checks or MagicMock(run_all=MagicMock(return_value=None))
    cog._llm_check = llm or MagicMock(check=AsyncMock(return_value=None))
    return cog


def _msg(**over: Any) -> MagicMock:
    m = MagicMock()
    m.id = over.get("id", 100)
    m.guild.id = over.get("guild_id", SERVER_ID)
    m.author.bot = over.get("bot", False)
    m.author.id = over.get("author_id", 555)
    m.content = over.get("content", "hello there")
    m.embeds = over.get("embeds", [])
    m.attachments = over.get("attachments", [])
    m.message_snapshots = []  # flatten_forwarded_message reads this
    m.type = over.get("type", MessageType.default)
    m.reference = over.get("reference")
    m.mentions = over.get("mentions", [])
    m.channel = over.get("channel", MagicMock())
    return m


@pytest.fixture(autouse=True)
def _patch_pipeline(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    # Default: no member (so the reputable short-circuit is skipped) and a
    # capturable report_message.
    report = AsyncMock()
    monkeypatch.setattr(sd, "report_message", report)
    monkeypatch.setattr(sd, "member_from_message", AsyncMock(return_value=None))
    return report


class TestOnMessage:
    async def test_ignores_other_guild(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        _patch_pipeline: AsyncMock,
    ) -> None:
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.on_message(_msg(guild_id=999))
        _patch_pipeline.assert_not_awaited()
        assert await mongo_db.message_counts.count_documents({}) == 0

    async def test_ignores_bot_author(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        _patch_pipeline: AsyncMock,
    ) -> None:
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.on_message(_msg(bot=True))
        _patch_pipeline.assert_not_awaited()

    async def test_thread_created_message_is_tracked(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        cog = _make_cog(make_bot(db=mongo_db))
        reference = MagicMock(channel_id=77)
        msg = _msg(id=500, type=MessageType.thread_created, reference=reference)
        await cog.on_message(msg)
        assert cog._thread_creation_messages[500] == 77

    async def test_reputable_member_is_skipped(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        _patch_pipeline: AsyncMock,
    ) -> None:
        monkeypatch.setattr(
            sd, "member_from_message", AsyncMock(return_value=MagicMock())
        )
        monkeypatch.setattr(sd, "is_reputable", lambda _m: True)
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.on_message(_msg())
        _patch_pipeline.assert_not_awaited()

    async def test_rule_hit_reports_with_reason(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        _patch_pipeline: AsyncMock,
    ) -> None:
        checks = MagicMock(run_all=MagicMock(return_value="Obfuscated URL"))
        cog = _make_cog(make_bot(db=mongo_db), checks=checks)
        msg = _msg()
        await cog.on_message(msg)
        _patch_pipeline.assert_awaited_once_with(cog._ctx, msg, "Obfuscated URL")

    async def test_llm_detection_reports(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        _patch_pipeline: AsyncMock,
    ) -> None:
        llm = MagicMock(check=AsyncMock(return_value="DM lure"))
        cog = _make_cog(make_bot(db=mongo_db), llm=llm)
        msg = _msg()
        await cog.on_message(msg)
        _patch_pipeline.assert_awaited_once_with(
            cog._ctx, msg, "DM lure (AI Detection)"
        )

    async def test_high_message_count_skips_llm(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        _patch_pipeline: AsyncMock,
    ) -> None:
        await mongo_db.message_counts.insert_one({"_id": 555, "count": 30})
        llm = MagicMock(check=AsyncMock(return_value=None))
        cog = _make_cog(make_bot(db=mongo_db), llm=llm)
        await cog.on_message(_msg(author_id=555))
        llm.check.assert_not_awaited()
        _patch_pipeline.assert_not_awaited()

    async def test_ping_in_new_thread_reports(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        _patch_pipeline: AsyncMock,
    ) -> None:
        thread = MagicMock(spec=Thread)
        thread.owner_id = 555
        msg = _msg(
            author_id=555, mentions=[MagicMock()], reference=None, channel=thread
        )
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.on_message(msg)
        _patch_pipeline.assert_awaited_once_with(
            cog._ctx, msg, "Pinged user in new thread"
        )

    async def test_llm_exception_is_reported(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        bot = make_bot(db=mongo_db)
        llm = MagicMock(check=AsyncMock(side_effect=RuntimeError("boom")))
        cog = _make_cog(bot, llm=llm)
        await cog.on_message(_msg())
        bot.report_error.assert_awaited()

    async def test_no_guild_ignored(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        _patch_pipeline: AsyncMock,
    ) -> None:
        msg = _msg()
        msg.guild = None
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.on_message(msg)
        _patch_pipeline.assert_not_awaited()

    async def test_empty_message_ignored(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        _patch_pipeline: AsyncMock,
    ) -> None:
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.on_message(_msg(content="", embeds=[], attachments=[]))
        _patch_pipeline.assert_not_awaited()

    async def test_clean_message_passes_llm_without_report(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        _patch_pipeline: AsyncMock,
    ) -> None:
        llm = MagicMock(check=AsyncMock(return_value=None))
        cog = _make_cog(make_bot(db=mongo_db), llm=llm)
        await cog.on_message(_msg())
        llm.check.assert_awaited_once()
        _patch_pipeline.assert_not_awaited()

    async def test_on_message_edit_reroutes_through_on_message(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        _patch_pipeline: AsyncMock,
    ) -> None:
        checks = MagicMock(run_all=MagicMock(return_value="Edited into a scam"))
        cog = _make_cog(make_bot(db=mongo_db), checks=checks)
        after = _msg()
        await cog.on_message_edit(_msg(), after)
        _patch_pipeline.assert_awaited_once_with(cog._ctx, after, "Edited into a scam")


class TestIncrementMessageCount:
    async def test_counts_increment_per_user(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        cog = _make_cog(make_bot(db=mongo_db))
        user = MagicMock(id=42)
        assert await cog._increment_message_count(user) == 1
        assert await cog._increment_message_count(user) == 2


class TestMemberListeners:
    async def test_ban_resolves_reports(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.scam_reports.insert_many(
            [
                {"guild_id": 1, "user_id": 5, "report_id": 10},
                {"guild_id": 1, "user_id": 5, "report_id": 11},
            ]
        )
        resolve = AsyncMock()
        monkeypatch.setattr(sd, "resolve_report", resolve)
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.on_member_ban(MagicMock(id=1), MagicMock(id=5))
        assert resolve.await_count == 2

    async def test_timeout_updates_reports(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.scam_reports.insert_one(
            {"guild_id": 1, "user_id": 5, "report_id": 10}
        )
        update = AsyncMock()
        monkeypatch.setattr(sd, "update_report", update)
        before = MagicMock()
        before.is_timed_out.return_value = False
        after = MagicMock()
        after.is_timed_out.return_value = True
        after.guild.id = 1
        after.id = 5
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.on_member_update(before, after)
        update.assert_awaited_once()

    async def test_no_timeout_change_is_noop(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        update = AsyncMock()
        monkeypatch.setattr(sd, "update_report", update)
        member = MagicMock()
        member.is_timed_out.return_value = False
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.on_member_update(member, member)
        update.assert_not_awaited()

    async def test_timeout_change_without_reports_is_noop(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        update = AsyncMock()
        monkeypatch.setattr(sd, "update_report", update)
        before = MagicMock()
        before.is_timed_out.return_value = False
        after = MagicMock()
        after.is_timed_out.return_value = True
        after.guild.id = 1
        after.id = 5  # no reports stored for this user
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.on_member_update(before, after)
        update.assert_not_awaited()


class TestThreadAndDeleteListeners:
    async def test_thread_lock_triggers_removal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        removed = AsyncMock()
        monkeypatch.setattr(sd, "on_thread_removed", removed)
        cog = _make_cog(make_bot())
        before = MagicMock(locked=False)
        after = MagicMock(locked=True, id=300)
        await cog.on_thread_update(before, after)
        removed.assert_awaited_once_with(cog._ctx, 300, "Thread has been locked.")

    async def test_already_locked_thread_is_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        removed = AsyncMock()
        monkeypatch.setattr(sd, "on_thread_removed", removed)
        cog = _make_cog(make_bot())
        await cog.on_thread_update(MagicMock(locked=True), MagicMock(locked=True))
        removed.assert_not_awaited()

    async def test_raw_message_delete_delegates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        starter = AsyncMock()
        deleted = AsyncMock()
        monkeypatch.setattr(sd, "check_thread_starter_deleted", starter)
        monkeypatch.setattr(sd, "on_message_delete", deleted)
        cog = _make_cog(make_bot())
        await cog.on_raw_message_delete(MagicMock(message_id=99))
        starter.assert_awaited_once()
        deleted.assert_awaited_once_with(cog._ctx, 99)

    async def test_raw_thread_delete_delegates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        removed = AsyncMock()
        monkeypatch.setattr(sd, "on_thread_removed", removed)
        cog = _make_cog(make_bot())
        await cog.on_raw_thread_delete(MagicMock(thread_id=300))
        removed.assert_awaited_once_with(cog._ctx, 300, "Thread has been deleted.")

    async def test_bulk_message_delete_delegates_per_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sd, "check_thread_starter_deleted", AsyncMock())
        deleted = AsyncMock()
        monkeypatch.setattr(sd, "on_message_delete", deleted)
        cog = _make_cog(make_bot())
        await cog.on_raw_bulk_message_delete(MagicMock(message_ids={1, 2, 3}))
        assert deleted.await_count == 3


class TestContextMenuCallbacks:
    async def test_manual_message_report_delegates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manual = AsyncMock()
        monkeypatch.setattr(sd, "manual_message_report", manual)
        cog = _make_cog(make_bot())
        interaction, message = MagicMock(), MagicMock()
        await cog._manual_message_report(interaction, message)
        manual.assert_awaited_once_with(cog._ctx, interaction, message)

    async def test_manual_user_report_delegates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manual = AsyncMock()
        monkeypatch.setattr(sd, "manual_user_report", manual)
        cog = _make_cog(make_bot())
        interaction, user = MagicMock(), MagicMock()
        await cog._manual_user_report(interaction, user)
        manual.assert_awaited_once_with(cog._ctx, interaction, user)


class TestReportUserCommand:
    async def test_delegates_to_manual_user_report(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manual = AsyncMock()
        monkeypatch.setattr(sd, "manual_user_report", manual)
        cog = _make_cog(make_bot())
        interaction = MagicMock()
        user = MagicMock()
        cmd: Any = cog.report_user
        await cmd.callback(cog, interaction, user, "spam")
        manual.assert_awaited_once_with(cog._ctx, interaction, user, "spam")
