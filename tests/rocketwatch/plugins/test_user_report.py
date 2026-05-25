from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from discord import Member
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.scam_detection import user_report as ur
from rocketwatch.plugins.scam_detection.common import AutomodAction
from tests.lib.discord_harness import make_bot
from tests.lib.scam_detection_harness import ScriptedSentinel, make_ctx


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
