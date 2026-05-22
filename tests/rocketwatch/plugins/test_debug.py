from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.abc import Messageable

from rocketwatch.plugins.debug.debug import Debug
from tests.lib.discord_harness import make_bot, make_interaction
from tests.lib.scripted_rocketpool import ScriptedRocketPool


def _channel() -> MagicMock:
    ch = MagicMock(spec=Messageable)
    ch.send = AsyncMock()
    ch.fetch_message = AsyncMock()
    return ch


def _content(interaction: Any) -> str:
    call = interaction.followup.send.call_args
    return call.kwargs.get("content") or (call.args[0] if call.args else "")


class TestPurgeMinipools:
    async def test_requires_confirm(self) -> None:
        bot = make_bot()
        bot.db.minipools.drop = AsyncMock()
        cog = Debug(bot)
        interaction = make_interaction()
        await cog.purge_minipools.callback(cog, interaction, confirm=False)
        bot.db.minipools.drop.assert_not_awaited()
        assert "Not running" in _content(interaction)

    async def test_confirm_drops_collection(self) -> None:
        bot = make_bot()
        bot.db.minipools.drop = AsyncMock()
        cog = Debug(bot)
        interaction = make_interaction()
        await cog.purge_minipools.callback(cog, interaction, confirm=True)
        bot.db.minipools.drop.assert_awaited_once()
        assert _content(interaction) == "Done"


class TestSyncCommands:
    async def test_calls_bot_sync(self) -> None:
        bot = make_bot()
        bot.sync_commands = AsyncMock()
        cog = Debug(bot)
        interaction = make_interaction()
        await cog.sync_commands.callback(cog, interaction)
        bot.sync_commands.assert_awaited_once()
        assert _content(interaction) == "Done"


class TestTalk:
    async def test_sends_message_to_channel(self) -> None:
        bot = make_bot()
        channel = _channel()
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        cog = Debug(bot)
        interaction = make_interaction()
        await cog.talk.callback(cog, interaction, channel_id="123", message="hi")
        channel.send.assert_awaited_once_with("hi")
        assert _content(interaction) == "Done"


class TestAnnounce:
    async def test_sends_embed_with_timestamp(self) -> None:
        bot = make_bot()
        channel = _channel()
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        cog = Debug(bot)
        interaction = make_interaction()
        await cog.announce.callback(
            cog, interaction, channel_id="123", message="big news"
        )
        embed = channel.send.call_args.kwargs["embed"]
        assert embed.title == "Announcement"
        assert embed.description == "big news"
        assert any(f.name == "Timestamp" for f in embed.fields)


class TestDeleteMsg:
    async def test_fetches_and_deletes(self) -> None:
        bot = make_bot()
        channel = _channel()
        msg = MagicMock()
        msg.delete = AsyncMock()
        channel.fetch_message = AsyncMock(return_value=msg)
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        cog = Debug(bot)
        interaction = make_interaction()
        await cog.delete_msg.callback(
            cog, interaction, message_url="https://discord.com/channels/1/222/333"
        )
        channel.fetch_message.assert_awaited_once_with(333)
        msg.delete.assert_awaited_once()
        assert _content(interaction) == "Done"


class TestEditEmbed:
    async def test_edits_first_embed_description(self) -> None:
        bot = make_bot()
        channel = _channel()
        embed = MagicMock()
        msg = MagicMock()
        msg.embeds = [embed]
        msg.edit = AsyncMock()
        channel.fetch_message = AsyncMock(return_value=msg)
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        cog = Debug(bot)
        interaction = make_interaction()
        await cog.edit_embed.callback(
            cog,
            interaction,
            message_url="https://discord.com/channels/1/222/333",
            new_description="updated",
        )
        assert embed.description == "updated"
        msg.edit.assert_awaited_once()


class TestDebugTransaction:
    async def test_reports_revert_reason(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from rocketwatch.plugins.debug import debug as debug_module

        monkeypatch.setattr(
            debug_module.w3,
            "eth",
            AsyncMock(get_transaction=AsyncMock(return_value={"hash": "0x"})),
            raising=False,
        )
        monkeypatch.setattr(
            scripted_rp,
            "get_revert_reason",
            AsyncMock(return_value="out of gas"),
            raising=False,
        )
        cog = Debug(make_bot())
        interaction = make_interaction()
        await cog.debug_transaction.callback(cog, interaction, txn_hash="0xabc")
        assert "out of gas" in _content(interaction)

    async def test_no_revert_reason(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from rocketwatch.plugins.debug import debug as debug_module

        monkeypatch.setattr(
            debug_module.w3,
            "eth",
            AsyncMock(get_transaction=AsyncMock(return_value={"hash": "0x"})),
            raising=False,
        )
        monkeypatch.setattr(
            scripted_rp,
            "get_revert_reason",
            AsyncMock(return_value=None),
            raising=False,
        )
        cog = Debug(make_bot())
        interaction = make_interaction()
        await cog.debug_transaction.callback(cog, interaction, txn_hash="0xabc")
        assert "No revert reason" in _content(interaction)


class TestGetRoles:
    async def test_writes_role_file(self) -> None:
        bot = make_bot()
        guild = MagicMock()
        guild.name = "RP"
        guild.id = 1
        role = MagicMock()
        role.name = "Member"
        role.id = 99
        guild.roles = [role]
        bot.get_guild = MagicMock(return_value=guild)
        cog = Debug(bot)
        interaction = make_interaction()
        await cog.get_roles.callback(cog, interaction, guild_id="1")
        assert "file" in interaction.followup.send.call_args.kwargs

    async def test_reports_exception_on_missing_guild(self) -> None:
        bot = make_bot()
        bot.get_guild = MagicMock(return_value=None)  # assert None → AssertionError
        cog = Debug(bot)
        interaction = make_interaction()
        await cog.get_roles.callback(cog, interaction, guild_id="1")
        # Exception path renders a repr in a code block.
        assert "```" in _content(interaction)


class TestGetMembersOfRole:
    async def test_writes_member_file(self) -> None:
        bot = make_bot()
        guild = MagicMock()
        guild.name = "RP"
        guild.id = 1
        member = MagicMock()
        member.name = "alice"
        member.discriminator = "0001"
        member.id = 7
        role = MagicMock()
        role.name = "Member"
        role.id = 99
        role.members = [member]
        bot.get_or_fetch_guild = AsyncMock(return_value=guild)
        bot.get_or_fetch_role = AsyncMock(return_value=role)
        cog = Debug(bot)
        interaction = make_interaction()
        await cog.get_members_of_role.callback(
            cog, interaction, guild_id="1", role_id="99"
        )
        assert "file" in interaction.followup.send.call_args.kwargs

    async def test_reports_exception(self) -> None:
        bot = make_bot()
        bot.get_or_fetch_guild = AsyncMock(side_effect=RuntimeError("boom"))
        cog = Debug(bot)
        interaction = make_interaction()
        await cog.get_members_of_role.callback(
            cog, interaction, guild_id="1", role_id="99"
        )
        assert "```" in _content(interaction)


class TestRestoreSupportTemplate:
    async def test_rejects_unparseable_message(self) -> None:
        bot = make_bot()
        channel = _channel()
        embed = MagicMock()
        embed.title = "Template"
        embed.description = "Some body\nwith no edit footer"
        msg = MagicMock()
        msg.embeds = [embed]
        channel.fetch_message = AsyncMock(return_value=msg)
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        cog = Debug(bot)
        interaction = make_interaction()
        await cog.restore_support_template.callback(
            cog,
            interaction,
            template_name="welcome",
            message_url="https://discord.com/channels/1/2/3",
        )
        assert "Failed to restore" in _content(interaction)

    async def test_restores_parseable_template(self) -> None:
        bot = make_bot()
        channel = _channel()
        embed = MagicMock()
        embed.title = "Welcome Template"
        embed.description = (
            "Title line\n"
            "Body line\n"
            "footer separator\n"
            "Last Edited by <@42> <t:1700000000:R>"
        )
        msg = MagicMock()
        msg.embeds = [embed]
        channel.fetch_message = AsyncMock(return_value=msg)
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        user = MagicMock()
        user.id = 42
        user.name = "alice"
        bot.get_or_fetch_user = AsyncMock(return_value=user)
        bot.db.support_bot_dumps.insert_one = AsyncMock()
        bot.db.support_bot.insert_one = AsyncMock()

        cog = Debug(bot)
        interaction = make_interaction()
        await cog.restore_support_template.callback(
            cog,
            interaction,
            template_name="welcome",
            message_url="https://discord.com/channels/1/2/3",
        )
        bot.db.support_bot.insert_one.assert_awaited_once()
        bot.db.support_bot_dumps.insert_one.assert_awaited_once()
        assert _content(interaction) == "Done"
