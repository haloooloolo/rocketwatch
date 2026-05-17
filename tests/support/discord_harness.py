from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from discord import Embed


class RecordingInteraction(MagicMock):
    """A discord.Interaction stand-in whose response/followup calls are recorded."""


def make_interaction(
    *,
    user_id: int = 42,
    user_name: str = "tester",
    channel_id: int = 1,
    guild_id: int = 99,
) -> RecordingInteraction:
    interaction = RecordingInteraction()
    interaction.user.id = user_id
    interaction.user.display_name = user_name
    interaction.channel_id = channel_id
    interaction.guild_id = guild_id
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def captured_embed(interaction: RecordingInteraction) -> Embed:
    """Return the Embed that the cog sent via response or followup."""
    for sender in (interaction.response.send_message, interaction.followup.send):
        call = sender.call_args
        if call is None:
            continue
        embed = call.kwargs.get("embed")
        if embed is None and call.args:
            # Some call sites pass the embed positionally.
            for arg in call.args:
                if isinstance(arg, Embed):
                    embed = arg
                    break
        if isinstance(embed, Embed):
            return embed
    raise AssertionError(
        "Cog did not send any embed. "
        f"response.send_message calls: {interaction.response.send_message.call_args_list!r}; "
        f"followup.send calls: {interaction.followup.send.call_args_list!r}"
    )


def make_bot(*, db: Any = None) -> MagicMock:
    """A minimal RocketWatch stand-in. Pass `db=` for cogs that read `bot.db`."""
    bot = MagicMock()
    bot.db = db if db is not None else MagicMock()
    return bot


async def run_command(
    cog: Any,
    command_name: str,
    interaction: RecordingInteraction,
    *args: Any,
    **kwargs: Any,
) -> Embed:
    """Invoke `cog.<command_name>` and return the Embed it sent."""
    cmd = getattr(cog, command_name)
    callback = getattr(cmd, "callback", cmd)
    await callback(cog, interaction, *args, **kwargs)
    return captured_embed(interaction)
