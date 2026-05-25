from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord import Guild, Member
from discord.abc import Messageable
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.scam_detection import user_report as ur
from rocketwatch.plugins.scam_detection.common import AutomodAction
from rocketwatch.plugins.scam_detection.views import ReportReviewView
from tests.lib.discord_harness import make_bot
from tests.lib.scam_detection_harness import ScriptedSentinel, make_ctx

Db = AsyncDatabase[dict[str, Any]]


def _report_channel(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    channel = MagicMock(spec=Messageable)
    channel.send = AsyncMock(return_value=MagicMock(id=200, jump_url="http://r"))
    monkeypatch.setattr(ur, "get_report_channel", AsyncMock(return_value=channel))
    return channel


def _make_member(**over: Any) -> MagicMock:
    m = MagicMock(spec=Member)
    m.id = over.get("id", 1)
    m.display_name = over.get("display_name", "scammer")
    m.mention = "<@1>"
    m.created_at = datetime(2024, 1, 1, tzinfo=UTC)
    m.joined_at = over.get("joined_at", datetime(2024, 6, 1, tzinfo=UTC))
    role0 = MagicMock()
    role0.mention = "@everyone"
    role1 = MagicMock()
    role1.mention = "@member"
    m.roles = [role0, role1]
    m.display_avatar.url = "http://avatar"
    m.guild.id = over.get("guild_id", 9)
    return m


class TestGenerateReportEmbed:
    def test_includes_user_metadata(self) -> None:
        embed = ur._generate_report_embed(_make_member(), "impersonation")
        assert embed.description is not None
        assert "impersonation" in embed.description
        assert "scammer" in embed.description
        # roles[1:] is rendered, skipping @everyone.
        assert "@member" in embed.description

    def test_no_joined_at_omits_joined_line(self) -> None:
        embed = ur._generate_report_embed(_make_member(joined_at=None), "spam")
        assert embed.description is not None
        assert "**Joined**" not in embed.description


class TestComposeManualReason:
    def test_appends_reporter_mention(self) -> None:
        interaction = MagicMock()
        interaction.user.mention = "<@42>"
        assert ur._compose_manual_reason(interaction, "looks fake") == (
            "looks fake (reported by <@42>)"
        )


class TestClaimUserReport:
    async def test_claim_is_idempotent(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        ctx = make_ctx(db=mongo_db)
        assert await ur._claim_user_report(ctx, 9, 1) is True
        assert await ur._claim_user_report(ctx, 9, 1) is False
        await ur._release_claim(ctx, 9, 1)
        assert await ur._claim_user_report(ctx, 9, 1) is True


class TestFinalizeReport:
    async def test_writes_full_doc(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        ctx = make_ctx(db=mongo_db)
        await ur._claim_user_report(ctx, 9, 1)
        await ur._finalize_report(
            ctx, _make_member(), "impersonation", MagicMock(id=200)
        )
        doc = await mongo_db.scam_reports.find_one({"type": "user", "user_id": 1})
        assert doc is not None
        assert doc["reason"] == "impersonation"
        assert doc["report_id"] == 200
        assert doc["content"] == "scammer"


class TestRunUserAutomod:
    async def test_timeout_adds_action(self) -> None:
        ctx = make_ctx()
        actions = await ur.run_user_automod(ctx, _make_member(), "scam")
        assert actions == {AutomodAction.MEMBER_TIMED_OUT}

    async def test_no_timeout_no_action(self) -> None:
        sentinel = ScriptedSentinel()
        sentinel.timeout_member = AsyncMock(return_value=False)
        ctx = make_ctx(sentinel=sentinel)
        actions = await ur.run_user_automod(ctx, _make_member(), "scam")
        assert actions == set()

    async def test_exception_is_reported(self) -> None:
        bot = make_bot()
        sentinel = ScriptedSentinel()
        sentinel.timeout_member = AsyncMock(side_effect=RuntimeError("boom"))
        ctx = make_ctx(bot=bot, sentinel=sentinel)
        actions = await ur.run_user_automod(ctx, _make_member(), "scam")
        assert actions == set()
        bot.report_error.assert_awaited()


class TestBuildReviewView:
    def test_returns_view_when_enabled(self) -> None:
        ctx = make_ctx(sentinel=ScriptedSentinel(enabled=True))
        assert isinstance(ur._build_review_view(ctx), ReportReviewView)

    def test_returns_none_when_disabled(self) -> None:
        ctx = make_ctx(sentinel=ScriptedSentinel(enabled=False))
        assert ur._build_review_view(ctx) is None


class TestUserReportReasonModal:
    async def test_on_submit_executes_with_stripped_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        execute = AsyncMock()
        monkeypatch.setattr(ur, "_execute_user_report", execute)
        modal = ur.UserReportReasonModal(make_ctx(), _make_member())
        modal.reason_field._value = "  spammer  "

        await modal.on_submit(MagicMock())

        execute.assert_awaited_once()
        assert execute.await_args is not None
        assert execute.await_args.args[3] == "spammer"


class TestManualUserReport:
    def _interaction(self) -> MagicMock:
        interaction = MagicMock()
        interaction.user.mention = "<@42>"
        interaction.response.send_message = AsyncMock()
        interaction.response.send_modal = AsyncMock()
        return interaction

    async def test_bot_rejected(self) -> None:
        interaction = self._interaction()
        member = _make_member()
        member.bot = True
        await ur.manual_user_report(make_ctx(), interaction, member)
        assert "Bots" in interaction.response.send_message.call_args.kwargs["content"]

    async def test_self_report_rejected(self) -> None:
        interaction = self._interaction()
        member = _make_member()
        member.bot = False
        interaction.user = member
        await ur.manual_user_report(make_ctx(), interaction, member)
        assert (
            "yourself" in interaction.response.send_message.call_args.kwargs["content"]
        )

    async def test_non_member_rejected(self) -> None:
        interaction = self._interaction()
        user = MagicMock()  # not spec=Member → isinstance(user, Member) is False
        user.bot = False
        await ur.manual_user_report(make_ctx(), interaction, user)
        assert (
            "Failed to report"
            in interaction.response.send_message.call_args.kwargs["content"]
        )

    async def test_no_reason_opens_modal(self) -> None:
        interaction = self._interaction()
        member = _make_member()
        member.bot = False
        await ur.manual_user_report(make_ctx(), interaction, member)
        interaction.response.send_modal.assert_awaited_once()
        assert isinstance(
            interaction.response.send_modal.call_args.args[0], ur.UserReportReasonModal
        )

    async def test_with_reason_executes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        execute = AsyncMock()
        monkeypatch.setattr(ur, "_execute_user_report", execute)
        interaction = self._interaction()
        member = _make_member()
        member.bot = False
        await ur.manual_user_report(make_ctx(), interaction, member, "looks fake")
        execute.assert_awaited_once()


class TestExecuteUserReport:
    def _interaction(self) -> MagicMock:
        interaction = MagicMock()
        interaction.user.mention = "<@42>"
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    async def test_claim_failure_notifies(self, mongo_db: Db) -> None:
        await mongo_db.scam_reports.insert_one(
            {"type": "user", "guild_id": 9, "user_id": 1}
        )
        ctx = make_ctx(bot=make_bot(db=mongo_db))
        interaction = self._interaction()
        await ur._execute_user_report(ctx, interaction, _make_member(), "bad")
        assert (
            "Failed to report" in interaction.followup.send.call_args.kwargs["content"]
        )

    async def test_reputable_reporter_runs_automod(
        self, mongo_db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = make_ctx(bot=make_bot(db=mongo_db))
        _report_channel(monkeypatch)
        monkeypatch.setattr(
            ur, "member_from_interaction", AsyncMock(return_value=MagicMock())
        )
        monkeypatch.setattr(ur, "is_reputable", lambda _m: True)
        automod = AsyncMock()
        monkeypatch.setattr(ur, "run_user_automod", automod)
        broadcast = AsyncMock()
        monkeypatch.setattr(ur, "broadcast_user_report", broadcast)
        interaction = self._interaction()

        await ur._execute_user_report(ctx, interaction, _make_member(), "bad")

        automod.assert_awaited_once()
        broadcast.assert_awaited_once()
        doc = await mongo_db.scam_reports.find_one({"type": "user", "user_id": 1})
        assert doc is not None and doc["report_id"] == 200
        assert "Thanks" in interaction.followup.send.call_args.kwargs["content"]

    async def test_non_reputable_reporter_skips_automod(
        self, mongo_db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = make_ctx(bot=make_bot(db=mongo_db))
        _report_channel(monkeypatch)
        monkeypatch.setattr(
            ur, "member_from_interaction", AsyncMock(return_value=MagicMock())
        )
        monkeypatch.setattr(ur, "is_reputable", lambda _m: False)
        automod = AsyncMock()
        monkeypatch.setattr(ur, "run_user_automod", automod)
        interaction = self._interaction()

        await ur._execute_user_report(ctx, interaction, _make_member(), "bad")

        automod.assert_not_awaited()


class TestReportUserFromPartnerBan:
    def _partner_guild(self) -> MagicMock:
        guild = MagicMock(spec=Guild)
        guild.name = "PartnerDAO"
        guild.id = 555
        return guild

    async def test_bot_skipped(self) -> None:
        bot = make_bot()
        banned = MagicMock()
        banned.bot = True
        await ur.report_user_from_partner_ban(
            make_ctx(bot=bot), self._partner_guild(), banned
        )
        bot.get_or_fetch_member.assert_not_called()

    async def test_member_not_found_returns(self) -> None:
        from discord import NotFound

        bot = make_bot()
        bot.get_or_fetch_member = AsyncMock(side_effect=NotFound(MagicMock(), "x"))
        banned = MagicMock()
        banned.bot = False
        banned.id = 1
        # should not raise
        await ur.report_user_from_partner_ban(
            make_ctx(bot=bot), self._partner_guild(), banned
        )

    async def test_reputable_member_skipped(
        self, mongo_db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bot = make_bot(db=mongo_db)
        bot.get_or_fetch_member = AsyncMock(return_value=_make_member(guild_id=1))
        monkeypatch.setattr(ur, "is_reputable", lambda _m: True)
        channel = _report_channel(monkeypatch)
        banned = MagicMock()
        banned.bot = False
        banned.id = 1
        await ur.report_user_from_partner_ban(
            make_ctx(bot=bot), self._partner_guild(), banned
        )
        channel.send.assert_not_awaited()

    async def test_existing_reports_annotated(
        self, mongo_db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bot = make_bot(db=mongo_db)
        bot.get_or_fetch_member = AsyncMock(return_value=_make_member(guild_id=1))
        monkeypatch.setattr(ur, "is_reputable", lambda _m: False)
        update = AsyncMock()
        monkeypatch.setattr(ur, "update_report", update)
        # rp guild id from baseline cfg is 1
        await mongo_db.scam_reports.insert_one(
            {"guild_id": 1, "user_id": 1, "report_id": 200}
        )
        banned = MagicMock()
        banned.bot = False
        banned.id = 1
        await ur.report_user_from_partner_ban(
            make_ctx(bot=bot), self._partner_guild(), banned
        )
        update.assert_awaited()

    async def test_new_report_created_and_automod_run(
        self, mongo_db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bot = make_bot(db=mongo_db)
        bot.get_or_fetch_member = AsyncMock(return_value=_make_member(guild_id=1))
        monkeypatch.setattr(ur, "is_reputable", lambda _m: False)
        _report_channel(monkeypatch)
        automod = AsyncMock()
        monkeypatch.setattr(ur, "run_user_automod", automod)
        banned = MagicMock()
        banned.bot = False
        banned.id = 1
        await ur.report_user_from_partner_ban(
            make_ctx(bot=bot), self._partner_guild(), banned
        )
        automod.assert_awaited_once()
        doc = await mongo_db.scam_reports.find_one({"type": "user", "user_id": 1})
        assert doc is not None and doc["report_id"] == 200
