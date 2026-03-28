from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord
from config import ApiConfig, Config, DiscordConfig

TEST_KEY_SECRET = "test-secret-key"
TEST_GUILD_ID = 123456


def make_test_config(**key_overrides) -> Config:
    key_defaults = dict(
        secret=TEST_KEY_SECRET,
        allowed_server_ids=[TEST_GUILD_ID],
        delete_message_max_age=900,
        lock_thread_max_age=3600,
        delete_thread_max_age=3600,
        timeout_member_max_duration=86400,
        kick_member_max_age=604800,
        ban_member_max_age=604800,
        max_actions_per_hour=100,
    )
    key_defaults.update(key_overrides)
    return Config(
        discord=DiscordConfig(token="fake-token"),
        api=ApiConfig(keys=[key_defaults]),
    )


def make_mock_member(
    user_id=1000,
    is_moderator=False,
    joined_at=None,
    is_timed_out=False,
    timed_out_until=None,
):
    member = MagicMock()
    member.id = user_id
    member.guild_permissions.moderate_members = is_moderator
    member.joined_at = joined_at or (datetime.now(UTC) - timedelta(hours=1))
    member.is_timed_out.return_value = is_timed_out
    member.timed_out_until = timed_out_until
    member.timeout = AsyncMock()
    member.kick = AsyncMock()
    member.ban = AsyncMock()
    return member


def make_mock_message(message_id=2000, created_at=None):
    message = MagicMock()
    message.id = message_id
    message.created_at = created_at or (datetime.now(UTC) - timedelta(minutes=5))
    message.delete = AsyncMock()
    return message


def make_mock_thread(thread_id=3000, created_at=None):
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.created_at = created_at or (datetime.now(UTC) - timedelta(minutes=5))
    thread.edit = AsyncMock()
    thread.delete = AsyncMock()
    return thread


def make_mock_channel(channel_id=4000):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.fetch_message = AsyncMock()
    return channel


def make_mock_guild(guild_id=TEST_GUILD_ID):
    guild = MagicMock()
    guild.id = guild_id
    guild.get_channel = MagicMock(return_value=None)
    guild.get_thread = MagicMock(return_value=None)
    guild.fetch_channel = AsyncMock()
    guild.fetch_member = AsyncMock()
    return guild


def make_mock_bot(guild=None):
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    return bot
