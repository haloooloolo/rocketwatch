from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.app_commands.errors import (
    AppCommandError,
    BotMissingPermissions,
    CheckFailure,
    CommandOnCooldown,
    MissingPermissions,
    NoPrivateMessage,
    TransformerError,
)

from rocketwatch.utils.command_tree import RWCommandTree, _channel_name


class TestChannelName:
    def test_returns_named_channel(self):
        interaction = MagicMock()
        interaction.channel.name = "general"
        assert _channel_name(interaction) == "general"

    def test_dm_has_no_name_falls_back(self):
        # DMChannel objects don't expose a .name; getattr returns None and the
        # helper substitutes "DM" so logging stays consistent.
        interaction = MagicMock()
        interaction.channel = MagicMock(spec=[])
        assert _channel_name(interaction) == "DM"

    def test_named_channel_with_falsy_name_falls_back(self):
        # An empty string is also treated as "no name available".
        interaction = MagicMock()
        interaction.channel.name = ""
        assert _channel_name(interaction) == "DM"


# ---- on_error error→message mapping ---------------------------------------


@pytest.fixture
def interaction():
    mock = MagicMock()
    mock.user = "vitalik"
    mock.guild = MagicMock()
    mock.guild.__str__ = lambda _: "Rocket Pool"
    mock.channel.name = "general"
    mock.command.name = "test_cmd"
    mock.followup.send = AsyncMock()
    return mock


@pytest.fixture
def tree():
    """Build a RWCommandTree-like mock with the parts on_error touches."""
    t = MagicMock(spec=RWCommandTree)
    t.client = MagicMock()
    t.client.report_error = AsyncMock()
    return t


async def _run_on_error(tree, interaction, error):
    """Call on_error as an unbound method so we don't have to construct a real tree."""
    await RWCommandTree.on_error(tree, interaction, error)
    # The send call is the user-facing message; return its content.
    interaction.followup.send.assert_awaited_once()
    return interaction.followup.send.await_args.kwargs.get("content") or (
        interaction.followup.send.await_args.args[0]
        if interaction.followup.send.await_args.args
        else ""
    )


class TestOnErrorMessageMapping:
    async def test_cooldown_message_mentions_retry_seconds(self, tree, interaction):
        err = CommandOnCooldown(MagicMock(), 7.4)
        msg = await _run_on_error(tree, interaction, err)
        # Message should advise the user to wait and surface the retry duration.
        assert "Slow down" in msg or "too fast" in msg
        assert "7" in msg

    async def test_missing_user_permissions_lists_them(self, tree, interaction):
        err = MissingPermissions(["manage_channels", "kick_members"])
        msg = await _run_on_error(tree, interaction, err)
        # Both missing perms should be visible to the user so they know what to ask for.
        assert "manage_channels" in msg
        assert "kick_members" in msg

    async def test_bot_missing_permissions_lists_them(self, tree, interaction):
        err = BotMissingPermissions(["send_messages"])
        msg = await _run_on_error(tree, interaction, err)
        # "I'm missing" distinguishes the bot-side gap from a user-side gap.
        assert "send_messages" in msg

    async def test_no_private_message_explains_server_only(self, tree, interaction):
        err = NoPrivateMessage()
        msg = await _run_on_error(tree, interaction, err)
        assert "server" in msg.lower() or "DM" in msg

    async def test_check_failure_generic_message(self, tree, interaction):
        err = CheckFailure()
        msg = await _run_on_error(tree, interaction, err)
        # Generic "you don't meet the requirements" — no specific permission named.
        assert "requirement" in msg.lower() or "permission" in msg.lower()

    async def test_transformer_error_names_offending_value(self, tree, interaction):
        # Construct a minimal TransformerError; we only care that its `.value`
        # appears in the resulting message.
        err = MagicMock(spec=TransformerError)
        err.value = "not_a_number"
        msg = await _run_on_error(tree, interaction, err)
        assert "not_a_number" in msg

    async def test_unknown_error_falls_back_to_generic(self, tree, interaction):
        # Any AppCommandError subclass we don't special-case gets the generic
        # "unexpected error, has been reported" message.
        err = AppCommandError("boom")
        msg = await _run_on_error(tree, interaction, err)
        assert "unexpected" in msg.lower() or "developer" in msg.lower()


class TestOnErrorReporting:
    async def test_reports_error_to_developer_channel(self, tree, interaction):
        err = AppCommandError("boom")
        await RWCommandTree.on_error(tree, interaction, err)
        # The error must always be reported to the dev channel via report_error,
        # regardless of which user-facing message is sent.
        tree.client.report_error.assert_awaited_once()

    async def test_send_failure_does_not_propagate(self, tree, interaction):
        # If the user-facing send fails (e.g. interaction expired), on_error must
        # still complete without raising — otherwise the error handler itself
        # becomes a new error source.
        interaction.followup.send.side_effect = RuntimeError("interaction gone")
        await RWCommandTree.on_error(tree, interaction, AppCommandError("boom"))
