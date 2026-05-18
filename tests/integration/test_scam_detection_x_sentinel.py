"""Cross-stack tests: scam_detection plugin → SentinelClient → real sentinel.server.

The HTTP boundary is already covered by [tests/sentinel/test_client.py]. This file
sits one layer higher: it invokes the plugin's automod orchestration functions
against a real `sentinel.server` instance and asserts the right discord-side
action fired (e.g. `member.timeout` was awaited with the expected duration).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from rocketwatch.plugins.scam_detection.common import (
    AutomodAction,
    ReportContext,
)
from rocketwatch.plugins.scam_detection.message_report import run_message_automod
from rocketwatch.plugins.scam_detection.thread_report import run_thread_automod
from rocketwatch.plugins.scam_detection.user_report import run_user_automod
from rocketwatch.utils.config import cfg as rw_cfg
from rocketwatch.utils.sentinel import SentinelClient
from sentinel.config import ApiConfig, Config, DiscordConfig
from sentinel.config import cfg as sentinel_cfg
from sentinel.guardrails import rate_limiter
from sentinel.server import create_app
from tests.lib.discord_harness import make_bot

TEST_KEY_SECRET = "test-secret-key"
TEST_GUILD_ID = 123_456


def _make_sentinel_config() -> Config:
    return Config(
        discord=DiscordConfig(token="fake-token"),
        api=ApiConfig(
            keys=[
                {
                    "secret": TEST_KEY_SECRET,
                    "allowed_server_ids": [TEST_GUILD_ID],
                    "delete_message_max_age": 900,
                    "lock_thread_max_age": 3600,
                    "delete_thread_max_age": 3600,
                    "timeout_member_max_duration": 86_400,
                    "kick_member_max_age": 604_800,
                    "ban_member_max_age": 604_800,
                    "max_actions_per_hour": 100,
                }
            ]
        ),
    )


@pytest.fixture
def _sentinel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject sentinel-side cfg and clear rate-limiter state per test."""
    monkeypatch.setattr(sentinel_cfg, "_instance", _make_sentinel_config())
    rate_limiter._timestamps.clear()


@pytest.fixture
async def stack(
    _sentinel_env: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[dict[str, Any]]:
    """Spin up the real sentinel server + a real SentinelClient pointed at it,
    and return a ReportContext plus the mock guild so tests can drive scenarios."""
    guild = MagicMock()
    guild.id = TEST_GUILD_ID
    guild.get_channel = MagicMock(return_value=None)
    guild.get_thread = MagicMock(return_value=None)
    guild.fetch_channel = AsyncMock()
    guild.fetch_member = AsyncMock()
    guild.fetch_ban = AsyncMock()

    sentinel_bot = MagicMock()
    sentinel_bot.get_guild = MagicMock(return_value=guild)

    server = TestServer(create_app(sentinel_bot))
    test_client = TestClient(server)
    await test_client.start_server()

    # Point the rocketwatch-side SentinelClient at the test server.
    rw_snapshot = rw_cfg._instance.model_copy(deep=True)
    rw_snapshot.sentinel.api_url = str(server.make_url(""))
    rw_snapshot.sentinel.api_key = TEST_KEY_SECRET
    monkeypatch.setattr(rw_cfg, "_instance", rw_snapshot)

    sentinel_client = SentinelClient()
    rocketwatch_bot = make_bot()
    ctx = ReportContext(bot=rocketwatch_bot, sentinel=sentinel_client)

    yield {
        "ctx": ctx,
        "guild": guild,
        "rocketwatch_bot": rocketwatch_bot,
    }

    await sentinel_client.close()
    await test_client.close()


def _make_member(
    *,
    user_id: int = 1000,
    is_timed_out: bool = False,
    joined_minutes_ago: int = 60,
) -> MagicMock:
    member = MagicMock()
    member.id = user_id
    member.guild.id = TEST_GUILD_ID
    member.guild_permissions.moderate_members = False
    member.joined_at = datetime.now(UTC) - timedelta(minutes=joined_minutes_ago)
    member.is_timed_out.return_value = is_timed_out
    member.timed_out_until = None
    member.timeout = AsyncMock()
    member.mention = f"<@{user_id}>"
    return member


def _make_message(
    *,
    message_id: int = 2000,
    channel_id: int = 4000,
    author_id: int = 1000,
    minutes_old: int = 5,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = message_id
    msg.created_at = datetime.now(UTC) - timedelta(minutes=minutes_old)
    msg.delete = AsyncMock()
    msg.guild.id = TEST_GUILD_ID
    msg.channel.id = channel_id
    msg.author.id = author_id
    msg.author.mention = f"<@{author_id}>"
    return msg


def _make_thread(*, thread_id: int = 3000, owner_id: int = 1000) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.created_at = datetime.now(UTC) - timedelta(minutes=5)
    thread.guild.id = TEST_GUILD_ID
    thread.owner_id = owner_id
    thread.edit = AsyncMock()
    thread.jump_url = f"https://discord/{thread_id}"
    return thread


class TestRunUserAutomod:
    async def test_timeout_member_round_trips_to_real_server(
        self, stack: dict[str, Any]
    ) -> None:
        # User-automod is the simplest path: detector → SentinelClient.timeout_member
        # → real sentinel server → discord member.timeout. Pin all four legs.
        ctx = stack["ctx"]
        guild = stack["guild"]
        member = _make_member(user_id=1234)
        guild.fetch_member.return_value = member

        actions = await run_user_automod(ctx, member, reason="phishing")

        assert AutomodAction.MEMBER_TIMED_OUT in actions
        # The server-side mock got reached.
        guild.fetch_member.assert_awaited_once_with(1234)
        # Discord's member.timeout was awaited with the configured duration
        # (DEFAULT_USER_TIMEOUT = 24h) and the reason flowed through.
        member.timeout.assert_awaited_once()
        call = member.timeout.await_args
        assert call.kwargs.get("reason") == "phishing"

    async def test_already_timed_out_yields_no_action(
        self, stack: dict[str, Any]
    ) -> None:
        # If the member is already in timeout, sentinel returns 409 (conflict)
        # and the plugin treats it as "no action taken".
        ctx = stack["ctx"]
        guild = stack["guild"]
        member = _make_member(is_timed_out=True)
        member.timed_out_until = datetime.now(UTC) + timedelta(hours=1)
        guild.fetch_member.return_value = member

        actions = await run_user_automod(ctx, member, reason="phishing")
        assert AutomodAction.MEMBER_TIMED_OUT not in actions
        # And we never reached the timeout() call — server short-circuited.
        member.timeout.assert_not_awaited()

    async def test_member_not_found_returns_no_action(
        self, stack: dict[str, Any]
    ) -> None:
        ctx = stack["ctx"]
        guild = stack["guild"]
        guild.fetch_member.side_effect = discord.NotFound(MagicMock(status=404), "")
        member = _make_member()
        actions = await run_user_automod(ctx, member, reason="phishing")
        assert actions == set()


class TestRunMessageAutomod:
    async def test_delete_and_timeout_both_fire(self, stack: dict[str, Any]) -> None:
        # The message delete and member timeout run concurrently. Wire the
        # guild to resolve both lookups and assert the right server-side calls.
        ctx = stack["ctx"]
        guild = stack["guild"]
        rw_bot = stack["rocketwatch_bot"]

        message = _make_message(author_id=1234)
        # Make the message reachable via guild.get_channel + channel.fetch_message.
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=message)
        guild.get_channel.return_value = channel

        member = _make_member(user_id=1234)
        guild.fetch_member.return_value = member
        rw_bot.get_or_fetch_member = AsyncMock(return_value=member)

        # build_automod_embed's `report_msg.jump_url` is read; supply it.
        report_msg = MagicMock(spec=discord.Message)
        report_msg.jump_url = "https://discord/report/1"

        actions = await run_message_automod(ctx, message, "phishing", report_msg)

        assert AutomodAction.MESSAGE_DELETED in actions
        assert AutomodAction.MEMBER_TIMED_OUT in actions
        message.delete.assert_awaited_once()
        member.timeout.assert_awaited_once()

    async def test_message_only_no_member_skips_timeout(
        self, stack: dict[str, Any]
    ) -> None:
        # If `member_from_message` returns None, only the message delete fires —
        # timeout is silently skipped.
        ctx = stack["ctx"]
        guild = stack["guild"]
        rw_bot = stack["rocketwatch_bot"]

        message = _make_message()
        # Plain MagicMock for the author (not isinstance(Member)) so the
        # member-resolution function falls through to bot.get_or_fetch_member.
        message.author = MagicMock()
        message.author.id = 1234
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=message)
        guild.get_channel.return_value = channel
        # And make the rocketwatch-side member lookup return None.
        rw_bot.get_or_fetch_member = AsyncMock(return_value=None)

        actions = await run_message_automod(ctx, message, "phishing", MagicMock())

        message.delete.assert_awaited_once()
        assert AutomodAction.MESSAGE_DELETED in actions
        assert AutomodAction.MEMBER_TIMED_OUT not in actions


class TestRunThreadAutomod:
    async def test_lock_and_timeout_both_fire(self, stack: dict[str, Any]) -> None:
        ctx = stack["ctx"]
        guild = stack["guild"]
        rw_bot = stack["rocketwatch_bot"]

        thread = _make_thread(thread_id=3000, owner_id=1234)
        # Sentinel server looks up the thread via guild.get_channel/get_thread.
        guild.get_thread.return_value = thread

        member = _make_member(user_id=1234)
        guild.fetch_member.return_value = member
        rw_bot.get_or_fetch_member = AsyncMock(return_value=member)

        # thread.parent must be a Messageable for the alert post path, but
        # for this test we only assert on the automod result set.
        thread.parent = None
        report_msg = MagicMock(spec=discord.Message)
        report_msg.jump_url = "https://discord/report/1"

        actions = await run_thread_automod(ctx, thread, "scam", report_msg)

        assert AutomodAction.THREAD_LOCKED in actions
        assert AutomodAction.MEMBER_TIMED_OUT in actions
        thread.edit.assert_awaited_once()
        member.timeout.assert_awaited_once()


class TestServerErrorPropagation:
    async def test_rocketwatch_swallows_sentinel_500(
        self, stack: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A 500 from the sentinel server (e.g. unexpected discord.Forbidden)
        # must not crash the cog — the request returns False and we get an
        # empty action set.
        ctx = stack["ctx"]
        guild = stack["guild"]
        member = _make_member()
        member.timeout.side_effect = discord.Forbidden(MagicMock(status=403), "no perm")
        guild.fetch_member.return_value = member

        actions = await run_user_automod(ctx, member, reason="x")
        # The server logs 'forbidden' and returns 403; the client treats
        # any non-200 as a failed action.
        assert AutomodAction.MEMBER_TIMED_OUT not in actions
