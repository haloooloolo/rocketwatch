from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord import Thread
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.scam_detection import views as v
from rocketwatch.plugins.scam_detection.views import ReportReviewView
from tests.lib.scam_detection_harness import ScriptedSentinel, make_ctx


def _interaction(cog: Any, db: Any) -> MagicMock:
    i = MagicMock()
    i.message.id = 200
    i.user.mention = "<@42>"
    i.response.send_message = AsyncMock()
    i.response.edit_message = AsyncMock()
    i.client.db = db
    i.client.get_cog = MagicMock(return_value=cog)
    i.client.get_or_fetch_member = AsyncMock(return_value=MagicMock())
    i.client.get_or_fetch_channel = AsyncMock()
    return i


def _cog(sentinel: ScriptedSentinel) -> MagicMock:
    cog = MagicMock()
    cog._ctx = make_ctx(sentinel=sentinel)
    return cog


def _note(resolve: AsyncMock) -> str:
    """The note text passed to resolve_report(ctx, report_id, note)."""
    assert resolve.await_args is not None
    return str(resolve.await_args.args[2])


class TestDismissButton:
    async def test_non_moderator_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(v, "member_from_interaction", AsyncMock(return_value=None))
        resolve = AsyncMock()
        monkeypatch.setattr(v, "resolve_report", resolve)
        interaction = _interaction(_cog(ScriptedSentinel()), MagicMock())

        await ReportReviewView().dismiss.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        resolve.assert_not_awaited()

    async def test_moderator_dismiss_lifts_timeout_and_resolves(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.scam_reports.insert_one(
            {"report_id": 200, "user_id": 5, "type": "user", "guild_id": 1}
        )
        member = MagicMock()
        member.guild.id = 1
        monkeypatch.setattr(
            v, "member_from_interaction", AsyncMock(return_value=member)
        )
        monkeypatch.setattr(v, "is_reputable", lambda _m: True)
        resolve = AsyncMock()
        monkeypatch.setattr(v, "resolve_report", resolve)

        cog = _cog(ScriptedSentinel())  # remove_timeout returns True by default
        interaction = _interaction(cog, mongo_db)

        await ReportReviewView().dismiss.callback(interaction)

        interaction.response.edit_message.assert_awaited_once_with(view=None)
        resolve.assert_awaited_once()
        note = _note(resolve)
        assert "Marked safe" in note
        assert "Timeout has been lifted" in note

    async def test_dismiss_unlocks_thread_and_deletes_warning(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A non-message/thread report that carries a thread channel + warning:
        # dismissing unlocks the thread and deletes the warning message.
        await mongo_db.scam_reports.insert_one(
            {
                "report_id": 200,
                "user_id": 5,
                "type": "user",
                "guild_id": 1,
                "channel_id": 7,
                "warning_id": 55,
            }
        )
        member = MagicMock()
        member.guild.id = 1
        monkeypatch.setattr(
            v, "member_from_interaction", AsyncMock(return_value=member)
        )
        monkeypatch.setattr(v, "is_reputable", lambda _m: True)
        resolve = AsyncMock()
        monkeypatch.setattr(v, "resolve_report", resolve)

        warning_msg = MagicMock()
        warning_msg.delete = AsyncMock()
        # spec=Thread satisfies both isinstance(Thread) and isinstance(Messageable).
        thread = MagicMock(spec=Thread)
        thread.fetch_message = AsyncMock(return_value=warning_msg)

        cog = _cog(ScriptedSentinel())  # unlock_thread returns True by default
        interaction = _interaction(cog, mongo_db)
        interaction.client.get_or_fetch_channel = AsyncMock(return_value=thread)

        await ReportReviewView().dismiss.callback(interaction)

        warning_msg.delete.assert_awaited_once()
        note = _note(resolve)
        assert "Thread has been unlocked" in note


class TestConfirmButton:
    async def test_non_admin_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            v, "member_from_interaction", AsyncMock(return_value=MagicMock())
        )
        monkeypatch.setattr(v, "is_admin", lambda _m: False)
        resolve = AsyncMock()
        monkeypatch.setattr(v, "resolve_report", resolve)
        interaction = _interaction(_cog(ScriptedSentinel()), MagicMock())

        await ReportReviewView().confirm.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        resolve.assert_not_awaited()

    async def test_admin_confirm_bans_and_resolves(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.scam_reports.insert_one(
            {"report_id": 200, "user_id": 5, "reason": "scam"}
        )
        member = MagicMock()
        member.guild.id = 1
        monkeypatch.setattr(
            v, "member_from_interaction", AsyncMock(return_value=member)
        )
        monkeypatch.setattr(v, "is_admin", lambda _m: True)
        resolve = AsyncMock()
        monkeypatch.setattr(v, "resolve_report", resolve)

        sentinel = ScriptedSentinel()  # ban_member returns True by default
        interaction = _interaction(_cog(sentinel), mongo_db)

        await ReportReviewView().confirm.callback(interaction)

        sentinel.ban_member.assert_awaited_once()
        resolve.assert_awaited_once()
        assert "User has been banned" in _note(resolve)

    async def test_admin_confirm_reports_failed_ban(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.scam_reports.insert_one(
            {"report_id": 200, "user_id": 5, "reason": "scam"}
        )
        member = MagicMock()
        member.guild.id = 1
        monkeypatch.setattr(
            v, "member_from_interaction", AsyncMock(return_value=member)
        )
        monkeypatch.setattr(v, "is_admin", lambda _m: True)
        resolve = AsyncMock()
        monkeypatch.setattr(v, "resolve_report", resolve)

        sentinel = ScriptedSentinel()
        sentinel.ban_member = AsyncMock(return_value=False)
        interaction = _interaction(_cog(sentinel), mongo_db)

        await ReportReviewView().confirm.callback(interaction)

        assert "Failed to ban user" in _note(resolve)

    async def test_confirm_without_resolvable_member_just_resolves(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.scam_reports.insert_one(
            {"report_id": 200, "user_id": 5, "reason": "scam"}
        )
        member = MagicMock()
        member.guild.id = 1
        monkeypatch.setattr(
            v, "member_from_interaction", AsyncMock(return_value=member)
        )
        monkeypatch.setattr(v, "is_admin", lambda _m: True)
        resolve = AsyncMock()
        monkeypatch.setattr(v, "resolve_report", resolve)

        sentinel = ScriptedSentinel()
        interaction = _interaction(_cog(sentinel), mongo_db)
        interaction.client.get_or_fetch_member = AsyncMock(return_value=None)

        await ReportReviewView().confirm.callback(interaction)

        sentinel.ban_member.assert_not_awaited()
        resolve.assert_awaited_once()
        assert "Confirmed by" in _note(resolve)
