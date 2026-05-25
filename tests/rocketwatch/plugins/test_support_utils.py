import re
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord import Member, User
from discord.app_commands import Choice
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.support_utils import support_utils as su
from rocketwatch.plugins.support_utils.support_utils import (
    AdminModal,
    AdminView,
    DeletableView,
    DeleteMessageButton,
    SupportGlobal,
    SupportUtils,
    _use,
    generate_template_embed,
    has_perms,
)
from rocketwatch.utils.config import cfg
from tests.lib.cfg import make_cfg
from tests.lib.discord_harness import captured_embed, make_bot, make_interaction

Db = AsyncDatabase[dict[str, Any]]


@pytest.fixture
def support_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    c = make_cfg()
    c.rocketpool.support.server_id = 1
    c.rocketpool.support.user_ids = [100]
    c.rocketpool.support.moderator_roles = [500]
    c.discord.owner.user_id = 1
    monkeypatch.setattr(cfg, "_instance", c)


async def _seed_template(
    db: Db, name: str, title: str = "Title", description: str = "Desc"
) -> None:
    await db.support_bot.insert_one(
        {"_id": name, "title": title, "description": description}
    )


async def _invoke(command: Any, *args: Any) -> None:
    """Call an app command's underlying callback directly (bypassing the tree)."""
    await command.callback(*args)


async def _autocomplete(cog: Any, interaction: Any, current: str) -> list[Any]:
    func: Any = cog.match_template
    return list(await func(interaction, current))


# --- has_perms --------------------------------------------------------------


class TestHasPerms:
    def _interaction(self, user: Any, guild_id: int | None = None) -> MagicMock:
        i = MagicMock()
        i.user = user
        if guild_id is None:
            i.guild = None
        else:
            i.guild = MagicMock()
            i.guild.id = guild_id
        return i

    def test_allowlisted_user_id(self, support_cfg: None) -> None:
        user = MagicMock(spec=User)
        user.id = 100
        assert has_perms(self._interaction(user)) is True

    def test_owner(self, support_cfg: None) -> None:
        user = MagicMock(spec=User)
        user.id = 1
        assert has_perms(self._interaction(user)) is True

    def test_member_with_moderator_role(self, support_cfg: None) -> None:
        member = MagicMock(spec=Member)
        member.id = 999
        role = MagicMock()
        role.id = 500
        member.roles = [role]
        assert has_perms(self._interaction(member)) is True

    def test_member_moderate_members_in_support_guild(self, support_cfg: None) -> None:
        member = MagicMock(spec=Member)
        member.id = 999
        member.roles = []
        member.guild_permissions.moderate_members = True
        assert has_perms(self._interaction(member, guild_id=1)) is True

    def test_member_moderate_members_wrong_guild_denied(
        self, support_cfg: None
    ) -> None:
        member = MagicMock(spec=Member)
        member.id = 999
        member.roles = []
        member.guild_permissions.moderate_members = True
        assert has_perms(self._interaction(member, guild_id=2)) is False

    def test_member_without_perms_denied(self, support_cfg: None) -> None:
        member = MagicMock(spec=Member)
        member.id = 999
        member.roles = []
        member.guild_permissions.moderate_members = False
        assert has_perms(self._interaction(member, guild_id=1)) is False

    def test_non_member_denied(self, support_cfg: None) -> None:
        user = MagicMock(spec=User)
        user.id = 999
        assert has_perms(self._interaction(user)) is False


# --- generate_template_embed ------------------------------------------------


class TestGenerateTemplateEmbed:
    async def test_missing_returns_none(self, mongo_db: Db) -> None:
        assert await generate_template_embed(mongo_db, "nope") is None

    async def test_basic_title_and_description(self, mongo_db: Db) -> None:
        await _seed_template(mongo_db, "faq", title="FAQ", description="hello")
        embed = await generate_template_embed(mongo_db, "faq")
        assert embed is not None
        assert embed.title == "FAQ"
        assert embed.description is not None
        assert "hello" in embed.description

    async def test_appends_last_edited(self, mongo_db: Db) -> None:
        await _seed_template(mongo_db, "faq", description="hello")
        await mongo_db.support_bot_dumps.insert_one(
            {"template": "faq", "ts": datetime.now(UTC), "author": {"id": 7}}
        )
        embed = await generate_template_embed(mongo_db, "faq")
        assert embed is not None and embed.description is not None
        assert "Last Edited" in embed.description
        assert "<@7>" in embed.description

    async def test_announcement_skips_last_edited(self, mongo_db: Db) -> None:
        await _seed_template(mongo_db, "announcement", description="d")
        await mongo_db.support_bot_dumps.insert_one(
            {"template": "announcement", "ts": datetime.now(UTC), "author": {"id": 7}}
        )
        embed = await generate_template_embed(mongo_db, "announcement")
        assert embed is not None and embed.description is not None
        assert "Last Edited" not in embed.description


# --- _use (shared by both cogs) ---------------------------------------------


class TestUse:
    async def test_missing_template_sends_error(self, mongo_db: Db) -> None:
        interaction = make_interaction()
        await _use(mongo_db, interaction, "nope", None)
        assert captured_embed(interaction).title == "Error"

    async def test_found_sends_template(self, mongo_db: Db) -> None:
        await _seed_template(mongo_db, "faq", title="FAQ", description="hi")
        interaction = make_interaction()
        await _use(mongo_db, interaction, "faq", None)
        assert captured_embed(interaction).title == "FAQ"

    async def test_mention_is_pinged(self, mongo_db: Db) -> None:
        await _seed_template(mongo_db, "faq")
        interaction = make_interaction()
        mention = MagicMock()
        mention.mention = "<@5>"
        await _use(mongo_db, interaction, "faq", mention)
        assert interaction.response.send_message.call_args.kwargs["content"] == "<@5>"

    async def test_embed_generation_failure_sends_error(
        self, mongo_db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _seed_template(mongo_db, "faq")
        monkeypatch.setattr(su, "generate_template_embed", AsyncMock(return_value=None))
        interaction = make_interaction()
        await _use(mongo_db, interaction, "faq", None)
        embed = captured_embed(interaction)
        assert embed.title == "Error"
        assert embed.description is not None and "generating" in embed.description


# --- SupportUtils template admin commands -----------------------------------


def _cog_and_interaction(db: Db, user_id: int = 100) -> tuple[SupportUtils, MagicMock]:
    cog = SupportUtils(make_bot(db=db))
    interaction = make_interaction()
    interaction.user = MagicMock(spec=User)
    interaction.user.id = user_id
    interaction.edit_original_response = AsyncMock()
    return cog, interaction


class TestSupportUtilsCommands:
    async def test_add_requires_permission(
        self, mongo_db: Db, support_cfg: None
    ) -> None:
        cog, interaction = _cog_and_interaction(mongo_db, user_id=42)
        await _invoke(SupportUtils.add, cog, interaction, "faq")
        assert captured_embed(interaction).title == "Error"
        assert await mongo_db.support_bot.find_one({"_id": "faq"}) is None

    async def test_add_creates_template(self, mongo_db: Db, support_cfg: None) -> None:
        cog, interaction = _cog_and_interaction(mongo_db)
        await _invoke(SupportUtils.add, cog, interaction, "faq")
        interaction.response.defer.assert_awaited_once()
        doc = await mongo_db.support_bot.find_one({"_id": "faq"})
        assert doc is not None
        assert doc["title"] == "Insert Title here"
        interaction.edit_original_response.assert_awaited()

    async def test_add_existing_errors(self, mongo_db: Db, support_cfg: None) -> None:
        await _seed_template(mongo_db, "faq")
        cog, interaction = _cog_and_interaction(mongo_db)
        await _invoke(SupportUtils.add, cog, interaction, "faq")
        embed = interaction.edit_original_response.call_args.kwargs["embed"]
        assert embed.title == "Error"

    async def test_edit_missing_errors(self, mongo_db: Db, support_cfg: None) -> None:
        cog, interaction = _cog_and_interaction(mongo_db)
        await _invoke(SupportUtils.edit, cog, interaction, "nope")
        embed = interaction.edit_original_response.call_args.kwargs["embed"]
        assert embed.title == "Error"

    async def test_edit_existing_shows_preview(
        self, mongo_db: Db, support_cfg: None
    ) -> None:
        await _seed_template(mongo_db, "faq", title="FAQ", description="d")
        cog, interaction = _cog_and_interaction(mongo_db)
        await _invoke(SupportUtils.edit, cog, interaction, "faq")
        embed = interaction.edit_original_response.call_args.kwargs["embed"]
        assert embed.title == "FAQ"

    async def test_edit_requires_permission(
        self, mongo_db: Db, support_cfg: None
    ) -> None:
        cog, interaction = _cog_and_interaction(mongo_db, user_id=42)
        await _invoke(SupportUtils.edit, cog, interaction, "faq")
        assert captured_embed(interaction).title == "Error"

    async def test_remove_deletes_template(
        self, mongo_db: Db, support_cfg: None
    ) -> None:
        await _seed_template(mongo_db, "faq")
        cog, interaction = _cog_and_interaction(mongo_db)
        await _invoke(SupportUtils.remove, cog, interaction, "faq")
        assert await mongo_db.support_bot.find_one({"_id": "faq"}) is None
        embed = interaction.edit_original_response.call_args.kwargs["embed"]
        assert embed.title == "Success"

    async def test_remove_missing_errors(self, mongo_db: Db, support_cfg: None) -> None:
        cog, interaction = _cog_and_interaction(mongo_db)
        await _invoke(SupportUtils.remove, cog, interaction, "nope")
        embed = interaction.edit_original_response.call_args.kwargs["embed"]
        assert embed.title == "Error"

    async def test_remove_requires_permission(
        self, mongo_db: Db, support_cfg: None
    ) -> None:
        cog, interaction = _cog_and_interaction(mongo_db, user_id=42)
        await _invoke(SupportUtils.remove, cog, interaction, "faq")
        assert captured_embed(interaction).title == "Error"

    async def test_list_sorted_by_name(self, mongo_db: Db) -> None:
        await _seed_template(mongo_db, "b")
        await _seed_template(mongo_db, "a")
        cog, interaction = _cog_and_interaction(mongo_db)
        await _invoke(SupportUtils.list, cog, interaction, "_id")
        embeds = interaction.edit_original_response.call_args.kwargs["embeds"]
        assert embeds[0].title == "Templates"
        desc = embeds[0].description
        assert desc.index("`a`") < desc.index("`b`")

    async def test_use_subcommand(self, mongo_db: Db) -> None:
        await _seed_template(mongo_db, "faq", title="FAQ")
        cog = SupportUtils(make_bot(db=mongo_db))
        interaction = make_interaction()
        await _invoke(SupportUtils.use, cog, interaction, "faq", None)
        assert captured_embed(interaction).title == "FAQ"

    async def test_autocomplete_filters_by_regex(self, mongo_db: Db) -> None:
        await _seed_template(mongo_db, "faq")
        await _seed_template(mongo_db, "rules")
        cog = SupportUtils(make_bot(db=mongo_db))
        interaction = make_interaction()
        choices = await _autocomplete(cog, interaction, "fa")
        assert all(isinstance(c, Choice) for c in choices)
        assert {c.value for c in choices} == {"faq"}


# --- SupportGlobal ----------------------------------------------------------


class TestSupportGlobal:
    async def test_use_command(self, mongo_db: Db) -> None:
        await _seed_template(mongo_db, "faq", title="FAQ")
        cog = SupportGlobal(make_bot(db=mongo_db))
        interaction = make_interaction()
        await _invoke(SupportGlobal._use, cog, interaction, "faq", None)
        assert captured_embed(interaction).title == "FAQ"

    async def test_autocomplete(self, mongo_db: Db) -> None:
        await _seed_template(mongo_db, "faq")
        cog = SupportGlobal(make_bot(db=mongo_db))
        interaction = make_interaction()
        choices = await _autocomplete(cog, interaction, "f")
        assert {c.value for c in choices} == {"faq"}


# --- AdminModal -------------------------------------------------------------


class TestAdminModal:
    async def test_conflict_attaches_pending_changes(self, mongo_db: Db) -> None:
        # stored template differs from the values the editor started with
        await _seed_template(mongo_db, "faq", title="NEW", description="NEWdesc")
        modal = AdminModal("OLD", "OLDdesc", mongo_db, "faq")
        interaction = make_interaction()
        interaction.response.edit_message = AsyncMock()
        original = MagicMock()
        original.add_files = AsyncMock()
        interaction.original_response = AsyncMock(return_value=original)

        await modal.on_submit(interaction)

        interaction.response.edit_message.assert_awaited_once()
        original.add_files.assert_awaited_once()
        # the template was left untouched
        doc = await mongo_db.support_bot.find_one({"_id": "faq"})
        assert doc is not None and doc["title"] == "NEW"

    async def test_success_updates_template_and_records_dump(
        self, mongo_db: Db
    ) -> None:
        await _seed_template(mongo_db, "faq", title="OLD", description="OLDdesc")
        modal = AdminModal("OLD", "OLDdesc", mongo_db, "faq")
        modal.title_field._value = "NewTitle"
        modal.description_field._value = "NewDesc"
        interaction = make_interaction()
        interaction.response.edit_message = AsyncMock()
        interaction.user.id = 7
        interaction.user.name = "mod"

        await modal.on_submit(interaction)

        doc = await mongo_db.support_bot.find_one({"_id": "faq"})
        assert doc is not None
        assert doc["title"] == "NewTitle"
        assert doc["description"] == "NewDesc"
        dump = await mongo_db.support_bot_dumps.find_one({"template": "faq"})
        assert dump is not None
        assert dump["new"]["title"] == "NewTitle"
        assert dump["author"]["id"] == 7
        interaction.response.edit_message.assert_awaited_once()

    async def test_missing_template_is_noop(self, mongo_db: Db) -> None:
        modal = AdminModal("OLD", "OLDdesc", mongo_db, "gone")
        interaction = make_interaction()
        interaction.response.edit_message = AsyncMock()
        await modal.on_submit(interaction)
        interaction.response.edit_message.assert_not_awaited()


# --- AdminView / DeletableView / DeleteMessageButton ------------------------


class TestViewsAndButtons:
    async def test_admin_view_edit_opens_modal(self, mongo_db: Db) -> None:
        await _seed_template(mongo_db, "faq")
        view = AdminView(mongo_db, "faq")
        interaction = make_interaction()
        interaction.response.send_modal = AsyncMock()
        await view.edit.callback(interaction)
        interaction.response.send_modal.assert_awaited_once()
        assert isinstance(interaction.response.send_modal.call_args.args[0], AdminModal)

    async def test_admin_view_edit_missing_template_noop(self, mongo_db: Db) -> None:
        view = AdminView(mongo_db, "gone")
        interaction = make_interaction()
        interaction.response.send_modal = AsyncMock()
        await view.edit.callback(interaction)
        interaction.response.send_modal.assert_not_awaited()

    async def test_delete_button_authorized_deletes(self) -> None:
        button = DeleteMessageButton(42)
        interaction = MagicMock()
        interaction.user.id = 42
        interaction.message.delete = AsyncMock()
        await button.callback(interaction)
        interaction.message.delete.assert_awaited_once()

    async def test_delete_button_unauthorized_noop(self) -> None:
        button = DeleteMessageButton(42)
        interaction = MagicMock()
        interaction.user.id = 999
        interaction.message.delete = AsyncMock()
        await button.callback(interaction)
        interaction.message.delete.assert_not_awaited()

    async def test_delete_button_from_custom_id(self) -> None:
        match = re.match(r"button:delete:(?P<id>\d+)", "button:delete:123")
        assert match is not None
        button = await DeleteMessageButton.from_custom_id(
            MagicMock(), MagicMock(), match
        )
        assert button.user_id == 123

    def test_deletable_view_has_delete_button(self) -> None:
        user = MagicMock()
        user.id = 7
        view = DeletableView(user)
        assert view.children
