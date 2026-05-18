from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from rocketwatch.plugins.scam_detection.common import (
    ReportColor,
    ReportContext,
    build_automod_embed,
    flatten_forwarded_message,
    is_admin,
    is_reputable,
    member_from_interaction,
    member_from_message,
    message_to_dict,
    resolve_report,
    update_report,
)
from rocketwatch.utils.config import cfg
from tests.lib.discord_harness import make_bot


def _attachment(
    *,
    filename: str = "file.png",
    url: str = "http://example/file.png",
    content_type: str = "image/png",
    size: int = 100,
) -> SimpleNamespace:
    return SimpleNamespace(
        filename=filename, url=url, content_type=content_type, size=size
    )


def _member(
    *,
    role_ids: list[int] | None = None,
    user_id: int = 100,
    can_ban: bool = False,
    can_moderate: bool = False,
) -> MagicMock:
    member = MagicMock()
    member.id = user_id
    member.roles = [SimpleNamespace(id=r) for r in (role_ids or [])]
    member.guild_permissions.ban_members = can_ban
    member.guild_permissions.moderate_members = can_moderate
    return member


# ---- is_admin / is_reputable ----------------------------------------------------


class TestIsAdmin:
    def test_ban_permission_implies_admin(self):
        assert is_admin(_member(can_ban=True)) is True

    def test_admin_role_implies_admin(self, monkeypatch: pytest.MonkeyPatch):
        # cfg.rocketpool.support.admin_roles is read at module-import time
        # into the ADMIN_ROLES set; patch the snapshot so the role check fires.
        from rocketwatch.plugins.scam_detection import common as mod

        monkeypatch.setattr(mod, "ADMIN_ROLES", {42})
        assert is_admin(_member(role_ids=[42])) is True

    def test_neither_permission_nor_role_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from rocketwatch.plugins.scam_detection import common as mod

        monkeypatch.setattr(mod, "ADMIN_ROLES", {42})
        assert is_admin(_member(role_ids=[1, 2, 3])) is False


class TestIsReputable:
    def test_admin_is_reputable(self):
        assert is_reputable(_member(can_ban=True)) is True

    def test_moderate_members_perm_is_reputable(self):
        assert is_reputable(_member(can_moderate=True)) is True

    def test_discord_owner_is_reputable(self):
        # `cfg.discord.owner.user_id` defaults to 1 in the baseline; match it.
        owner_id = cfg.discord.owner.user_id
        assert is_reputable(_member(user_id=owner_id)) is True

    def test_support_user_is_reputable(self, monkeypatch: pytest.MonkeyPatch):
        # `cfg.rocketpool.support.user_ids` is read live (not snapshotted).
        snapshot = cfg._instance.model_copy(deep=True)
        snapshot.rocketpool.support.user_ids = [999]
        monkeypatch.setattr(cfg, "_instance", snapshot)
        assert is_reputable(_member(user_id=999)) is True

    def test_moderator_role_is_reputable(self, monkeypatch: pytest.MonkeyPatch):
        from rocketwatch.plugins.scam_detection import common as mod

        monkeypatch.setattr(mod, "MODERATOR_ROLES", {7})
        assert is_reputable(_member(role_ids=[7])) is True

    def test_random_user_is_not_reputable(self, monkeypatch: pytest.MonkeyPatch):
        from rocketwatch.plugins.scam_detection import common as mod

        monkeypatch.setattr(mod, "MODERATOR_ROLES", set())
        monkeypatch.setattr(mod, "ADMIN_ROLES", set())
        assert is_reputable(_member(user_id=12345)) is False


# ---- message_to_dict ------------------------------------------------------------


class TestMessageToDict:
    def test_basic_text_only_payload(self):
        msg = MagicMock()
        msg.content = "hello"
        msg.embeds = []
        msg.attachments = []
        msg.message_snapshots = []
        assert message_to_dict(msg) == {"content": "hello"}

    def test_serialises_embeds_and_attachments(self):
        msg = MagicMock()
        msg.content = ""
        msg.embeds = [SimpleNamespace(title="t", description="d")]
        msg.attachments = [_attachment()]
        msg.message_snapshots = []
        data = message_to_dict(msg)
        assert "content" not in data
        assert data["embeds"] == [{"title": "t", "description": "d"}]
        assert data["attachments"][0]["filename"] == "file.png"

    def test_includes_forwarded_snapshots(self):
        snap = MagicMock()
        snap.content = "fwd"
        snap.embeds = []
        snap.attachments = []

        msg = MagicMock()
        msg.content = "main"
        msg.embeds = []
        msg.attachments = []
        msg.message_snapshots = [snap]
        data = message_to_dict(msg)
        assert data["content"] == "main"
        assert data["forwarded"] == [{"content": "fwd"}]


# ---- flatten_forwarded_message --------------------------------------------------


class TestFlattenForwardedMessage:
    def test_no_snapshots_is_a_no_op(self):
        msg = MagicMock()
        msg.content = "hi"
        msg.embeds = []
        msg.attachments = []
        msg.message_snapshots = []
        flatten_forwarded_message(msg)
        # Content unchanged; no merging happened.
        assert msg.content == "hi"

    def test_merges_content_embeds_attachments_in_place(self):
        snap = MagicMock()
        snap.content = "world"
        snap_embed = SimpleNamespace(title="snap", description=None)
        snap_att = _attachment(filename="snap.png")
        snap.embeds = [snap_embed]
        snap.attachments = [snap_att]

        msg = MagicMock()
        msg.content = "hello"
        main_embed = SimpleNamespace(title="main", description=None)
        main_att = _attachment(filename="main.png")
        msg.embeds = [main_embed]
        msg.attachments = [main_att]
        msg.message_snapshots = [snap]

        flatten_forwarded_message(msg)
        assert msg.content == "hello\n\nworld"
        # Order: main embed first, then snap embed.
        assert msg.embeds == [main_embed, snap_embed]
        assert msg.attachments == [main_att, snap_att]


# ---- member_from_message / member_from_interaction -----------------------------


class TestMemberFromMessage:
    async def test_returns_author_when_already_member(self):
        from discord import Member

        msg = MagicMock()
        msg.author = MagicMock(spec=Member)
        bot = MagicMock()
        assert await member_from_message(bot, msg) is msg.author

    async def test_returns_none_for_dm(self):
        # The function checks isinstance(author, Member); with a SimpleNamespace
        # the check fails. Combined with guild=None, the function returns None.
        msg = MagicMock()
        msg.author = SimpleNamespace(id=1)
        msg.guild = None

        bot = MagicMock()
        assert await member_from_message(bot, msg) is None

    async def test_fetches_member_when_guild_present(self):
        msg = MagicMock()
        msg.author = SimpleNamespace(id=5)
        msg.guild = SimpleNamespace(id=99)
        bot = MagicMock()
        fetched = MagicMock(name="fetched-member")
        bot.get_or_fetch_member = AsyncMock(return_value=fetched)

        result = await member_from_message(bot, msg)
        assert result is fetched
        bot.get_or_fetch_member.assert_awaited_once_with(99, 5)


class TestMemberFromInteraction:
    async def test_returns_user_when_already_member(self):
        from discord import Member

        interaction = MagicMock()
        interaction.user = MagicMock(spec=Member)
        assert await member_from_interaction(interaction) is interaction.user

    async def test_returns_none_when_no_guild(self):
        interaction = MagicMock()
        interaction.user = SimpleNamespace(id=1)
        interaction.guild = None
        assert await member_from_interaction(interaction) is None


# ---- build_automod_embed --------------------------------------------------------


class TestBuildAutomodEmbed:
    def test_single_action_phrasing(self):
        msg = MagicMock()
        msg.jump_url = "https://discord.example/msg/1"
        embed = build_automod_embed(msg, ["deleted the message"])
        assert embed.title == ":hammer: Automated Moderation"
        assert embed.url == "https://discord.example/msg/1"
        assert embed.description == "Deleted the message."
        assert embed.color == ReportColor.ALERT

    def test_two_actions_use_and(self):
        msg = MagicMock()
        msg.jump_url = "https://discord.example"
        embed = build_automod_embed(msg, ["deleted the message", "timed out the user"])
        assert embed.description == "Deleted the message and timed out the user."

    def test_three_actions_comma_then_and(self):
        msg = MagicMock()
        msg.jump_url = "https://discord.example"
        embed = build_automod_embed(
            msg,
            ["deleted the message", "locked the thread", "timed out the user"],
        )
        assert (
            embed.description
            == "Deleted the message, locked the thread and timed out the user."
        )

    def test_url_action_skips_capitalisation(self):
        # When the first action looks like a URL, the cog leaves casing alone.
        msg = MagicMock()
        msg.jump_url = "https://discord.example"
        embed = build_automod_embed(msg, ["https://link.example"])
        # No capitalisation applied — first char stays lowercase.
        assert embed.description == "https://link.example."


# ---- update_report / resolve_report --------------------------------------------


class TestUpdateReport:
    async def test_appends_note_and_yellows_the_embed(self):
        from rocketwatch.utils.embeds import Embed

        existing = Embed()
        existing.color = ReportColor.ALERT
        existing.description = "Original alert."

        report_msg = MagicMock()
        report_msg.embeds = [existing]
        report_msg.edit = AsyncMock()

        from discord.abc import Messageable

        channel = MagicMock(spec=Messageable)
        channel.fetch_message = AsyncMock(return_value=report_msg)
        bot = make_bot()
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)

        await update_report(
            ReportContext(bot=bot, sentinel=MagicMock()), 123, "moderated"
        )

        report_msg.edit.assert_awaited_once()
        # The edit kwargs carry the updated embed with appended note.
        edited = report_msg.edit.await_args.kwargs["embed"]
        assert "moderated" in (edited.description or "")
        assert edited.color == ReportColor.WARN

    async def test_skips_already_resolved_reports(self):
        from rocketwatch.utils.embeds import Embed

        # If the report is already OK-coloured (resolved), update_report
        # should leave it alone.
        existing = Embed()
        existing.color = ReportColor.OK
        existing.description = "Already resolved."

        report_msg = MagicMock()
        report_msg.embeds = [existing]
        report_msg.edit = AsyncMock()

        from discord.abc import Messageable

        channel = MagicMock(spec=Messageable)
        channel.fetch_message = AsyncMock(return_value=report_msg)
        bot = make_bot()
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)

        await update_report(
            ReportContext(bot=bot, sentinel=MagicMock()), 123, "ignored"
        )
        report_msg.edit.assert_not_awaited()


class TestResolveReport:
    async def test_marks_embed_green_and_strips_view(self):
        from rocketwatch.utils.embeds import Embed

        existing = Embed()
        existing.color = ReportColor.ALERT
        existing.description = "Alert."

        report_msg = MagicMock()
        report_msg.embeds = [existing]
        report_msg.edit = AsyncMock()

        from discord.abc import Messageable

        channel = MagicMock(spec=Messageable)
        channel.fetch_message = AsyncMock(return_value=report_msg)
        bot = make_bot()
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)

        await resolve_report(ReportContext(bot=bot, sentinel=MagicMock()), 1, "handled")

        edited = report_msg.edit.await_args.kwargs["embed"]
        assert edited.color == ReportColor.OK
        # `view=None` tells discord to strip any attached buttons.
        assert report_msg.edit.await_args.kwargs["view"] is None
