import time
from collections import deque
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from unittest.mock import MagicMock

import discord
import pytest
from helpers import (
    TEST_GUILD_ID,
    TEST_KEY_SECRET,
    make_mock_channel,
    make_mock_member,
    make_mock_message,
    make_mock_thread,
)

from sentinel.guardrails import rate_limiter


def _not_found():
    return discord.NotFound(
        response=MagicMock(status=HTTPStatus.NOT_FOUND), message="Not Found"
    )


def _forbidden():
    return discord.Forbidden(
        response=MagicMock(status=HTTPStatus.FORBIDDEN), message="Forbidden"
    )


def _headers(key=TEST_KEY_SECRET):
    return {"X-Api-Key": key}


def _fill_rate_limiter(n=100):
    """Pre-fill the rate limiter to capacity for the test key."""
    now = time.monotonic()
    rate_limiter._timestamps[TEST_KEY_SECRET] = deque([now] * n)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


class TestAuthMiddleware:
    async def test_valid_key(self, client):
        channel = make_mock_channel()
        message = make_mock_message()
        channel.fetch_message.return_value = message
        client.mock_guild.get_channel.return_value = channel

        resp = await client.post(
            "/delete_message",
            json={
                "guild_id": TEST_GUILD_ID,
                "channel_id": 4000,
                "message_id": 2000,
                "reason": "test",
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK

    async def test_invalid_key(self, client):
        resp = await client.post(
            "/delete_message",
            json={
                "guild_id": TEST_GUILD_ID,
                "channel_id": 4000,
                "message_id": 2000,
            },
            headers=_headers("wrong-key"),
        )
        assert resp.status == HTTPStatus.UNAUTHORIZED
        body = await resp.json()
        assert body["error"] == "unauthorized"

    async def test_missing_key(self, client):
        resp = await client.post(
            "/delete_message",
            json={
                "guild_id": TEST_GUILD_ID,
                "channel_id": 4000,
                "message_id": 2000,
            },
        )
        assert resp.status == HTTPStatus.UNAUTHORIZED


# ---------------------------------------------------------------------------
# /delete_message
# ---------------------------------------------------------------------------


class TestDeleteMessage:
    async def test_success(self, client):
        channel = make_mock_channel()
        message = make_mock_message()
        channel.fetch_message.return_value = message
        client.mock_guild.get_channel.return_value = channel

        resp = await client.post(
            "/delete_message",
            json={
                "guild_id": TEST_GUILD_ID,
                "channel_id": 4000,
                "message_id": 2000,
                "reason": "spam",
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["action"] == "delete_message"
        message.delete.assert_awaited_once()

    async def test_guild_not_allowed(self, client):
        resp = await client.post(
            "/delete_message",
            json={
                "guild_id": 999999,
                "channel_id": 4000,
                "message_id": 2000,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY
        body = await resp.json()
        assert body["error"] == "guild_not_allowed"

    async def test_rate_limited(self, client):
        _fill_rate_limiter()

        resp = await client.post(
            "/delete_message",
            json={
                "guild_id": TEST_GUILD_ID,
                "channel_id": 4000,
                "message_id": 2000,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.TOO_MANY_REQUESTS
        body = await resp.json()
        assert body["error"] == "rate_limited"
        assert "retry_after_seconds" in body

    async def test_guild_not_found(self, client):
        client.mock_bot.get_guild.return_value = None
        resp = await client.post(
            "/delete_message",
            json={
                "guild_id": TEST_GUILD_ID,
                "channel_id": 4000,
                "message_id": 2000,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND
        body = await resp.json()
        assert body["error"] == "guild_not_found"

    async def test_channel_not_found(self, client):
        client.mock_guild.get_channel.return_value = None
        client.mock_guild.get_thread.return_value = None
        client.mock_guild.fetch_channel.side_effect = _not_found()

        resp = await client.post(
            "/delete_message",
            json={
                "guild_id": TEST_GUILD_ID,
                "channel_id": 4000,
                "message_id": 2000,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND
        body = await resp.json()
        assert body["error"] == "channel_not_found"

    async def test_channel_found_via_get_thread(self, client):
        channel = make_mock_channel()
        message = make_mock_message()
        channel.fetch_message.return_value = message
        client.mock_guild.get_channel.return_value = None
        client.mock_guild.get_thread.return_value = channel

        resp = await client.post(
            "/delete_message",
            json={
                "guild_id": TEST_GUILD_ID,
                "channel_id": 4000,
                "message_id": 2000,
                "reason": "test",
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK

    async def test_channel_found_via_fetch(self, client):
        channel = make_mock_channel()
        message = make_mock_message()
        channel.fetch_message.return_value = message
        client.mock_guild.get_channel.return_value = None
        client.mock_guild.get_thread.return_value = None
        client.mock_guild.fetch_channel.return_value = channel

        resp = await client.post(
            "/delete_message",
            json={
                "guild_id": TEST_GUILD_ID,
                "channel_id": 4000,
                "message_id": 2000,
                "reason": "test",
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK

    async def test_message_not_found(self, client):
        channel = make_mock_channel()
        channel.fetch_message.side_effect = _not_found()
        client.mock_guild.get_channel.return_value = channel

        resp = await client.post(
            "/delete_message",
            json={
                "guild_id": TEST_GUILD_ID,
                "channel_id": 4000,
                "message_id": 2000,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND
        body = await resp.json()
        assert body["error"] == "message_not_found"

    async def test_message_too_old(self, client):
        channel = make_mock_channel()
        message = make_mock_message(
            created_at=datetime.now(UTC) - timedelta(seconds=1000)
        )
        channel.fetch_message.return_value = message
        client.mock_guild.get_channel.return_value = channel

        resp = await client.post(
            "/delete_message",
            json={
                "guild_id": TEST_GUILD_ID,
                "channel_id": 4000,
                "message_id": 2000,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY
        body = await resp.json()
        assert body["error"] == "message_too_old"

    async def test_forbidden(self, client):
        channel = make_mock_channel()
        message = make_mock_message()
        message.delete.side_effect = _forbidden()
        channel.fetch_message.return_value = message
        client.mock_guild.get_channel.return_value = channel

        resp = await client.post(
            "/delete_message",
            json={
                "guild_id": TEST_GUILD_ID,
                "channel_id": 4000,
                "message_id": 2000,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.FORBIDDEN
        body = await resp.json()
        assert body["error"] == "missing_permissions"


# ---------------------------------------------------------------------------
# /lock_thread
# ---------------------------------------------------------------------------


class TestLockThread:
    async def test_success(self, client):
        thread = make_mock_thread()
        client.mock_guild.get_thread.return_value = thread

        resp = await client.post(
            "/lock_thread",
            json={
                "guild_id": TEST_GUILD_ID,
                "thread_id": 3000,
                "reason": "resolved",
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["action"] == "lock_thread"
        thread.edit.assert_awaited_once_with(
            locked=True, archived=True, reason="resolved"
        )

    async def test_guild_not_allowed(self, client):
        resp = await client.post(
            "/lock_thread",
            json={"guild_id": 999999, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_rate_limited(self, client):
        _fill_rate_limiter()
        resp = await client.post(
            "/lock_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.TOO_MANY_REQUESTS

    async def test_guild_not_found(self, client):
        client.mock_bot.get_guild.return_value = None
        resp = await client.post(
            "/lock_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND

    async def test_thread_not_found(self, client):
        client.mock_guild.get_thread.return_value = None
        client.mock_guild.fetch_channel.side_effect = _not_found()

        resp = await client.post(
            "/lock_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND
        body = await resp.json()
        assert body["error"] == "thread_not_found"

    async def test_thread_found_via_fetch(self, client):
        thread = make_mock_thread()
        client.mock_guild.get_thread.return_value = None
        client.mock_guild.fetch_channel.return_value = thread

        resp = await client.post(
            "/lock_thread",
            json={
                "guild_id": TEST_GUILD_ID,
                "thread_id": 3000,
                "reason": "test",
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK

    async def test_thread_too_old(self, client):
        thread = make_mock_thread(
            created_at=datetime.now(UTC) - timedelta(seconds=4000)
        )
        client.mock_guild.get_thread.return_value = thread

        resp = await client.post(
            "/lock_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY
        body = await resp.json()
        assert body["error"] == "thread_too_old"

    async def test_forbidden(self, client):
        thread = make_mock_thread()
        thread.edit.side_effect = _forbidden()
        client.mock_guild.get_thread.return_value = thread

        resp = await client.post(
            "/lock_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# /delete_thread
# ---------------------------------------------------------------------------


class TestDeleteThread:
    async def test_success(self, client):
        thread = make_mock_thread()
        client.mock_guild.get_thread.return_value = thread

        resp = await client.post(
            "/delete_thread",
            json={
                "guild_id": TEST_GUILD_ID,
                "thread_id": 3000,
                "reason": "spam",
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["action"] == "delete_thread"
        thread.delete.assert_awaited_once()

    async def test_thread_not_found(self, client):
        client.mock_guild.get_thread.return_value = None
        client.mock_guild.fetch_channel.side_effect = _not_found()

        resp = await client.post(
            "/delete_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND

    async def test_thread_too_old(self, client):
        thread = make_mock_thread(
            created_at=datetime.now(UTC) - timedelta(seconds=4000)
        )
        client.mock_guild.get_thread.return_value = thread

        resp = await client.post(
            "/delete_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_forbidden(self, client):
        thread = make_mock_thread()
        thread.delete.side_effect = _forbidden()
        client.mock_guild.get_thread.return_value = thread

        resp = await client.post(
            "/delete_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# /timeout_member
# ---------------------------------------------------------------------------


class TestTimeoutMember:
    async def test_success(self, client):
        member = make_mock_member()
        client.mock_guild.fetch_member.return_value = member

        resp = await client.post(
            "/timeout_member",
            json={
                "guild_id": TEST_GUILD_ID,
                "user_id": 1000,
                "duration_seconds": 3600,
                "reason": "spam",
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["action"] == "timeout_member"
        assert "until" in body
        member.timeout.assert_awaited_once()

    async def test_guild_not_allowed(self, client):
        resp = await client.post(
            "/timeout_member",
            json={
                "guild_id": 999999,
                "user_id": 1000,
                "duration_seconds": 3600,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_duration_exceeds_limit(self, client):
        resp = await client.post(
            "/timeout_member",
            json={
                "guild_id": TEST_GUILD_ID,
                "user_id": 1000,
                "duration_seconds": 90000,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY
        body = await resp.json()
        assert body["error"] == "duration_exceeds_limit"
        assert "max_seconds" in body

    async def test_duration_too_short(self, client):
        resp = await client.post(
            "/timeout_member",
            json={
                "guild_id": TEST_GUILD_ID,
                "user_id": 1000,
                "duration_seconds": 0,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY
        body = await resp.json()
        assert body["error"] == "duration_too_short"

    async def test_rate_limited(self, client):
        _fill_rate_limiter()
        resp = await client.post(
            "/timeout_member",
            json={
                "guild_id": TEST_GUILD_ID,
                "user_id": 1000,
                "duration_seconds": 3600,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.TOO_MANY_REQUESTS

    async def test_guild_not_found(self, client):
        client.mock_bot.get_guild.return_value = None
        resp = await client.post(
            "/timeout_member",
            json={
                "guild_id": TEST_GUILD_ID,
                "user_id": 1000,
                "duration_seconds": 3600,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND

    async def test_member_not_found(self, client):
        client.mock_guild.fetch_member.side_effect = _not_found()

        resp = await client.post(
            "/timeout_member",
            json={
                "guild_id": TEST_GUILD_ID,
                "user_id": 1000,
                "duration_seconds": 3600,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND
        body = await resp.json()
        assert body["error"] == "member_not_found"

    async def test_target_is_moderator(self, client):
        member = make_mock_member(is_moderator=True)
        client.mock_guild.fetch_member.return_value = member

        resp = await client.post(
            "/timeout_member",
            json={
                "guild_id": TEST_GUILD_ID,
                "user_id": 1000,
                "duration_seconds": 3600,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY
        body = await resp.json()
        assert body["error"] == "target_is_moderator"

    async def test_already_timed_out(self, client):
        until = datetime.now(UTC) + timedelta(hours=1)
        member = make_mock_member(is_timed_out=True, timed_out_until=until)
        client.mock_guild.fetch_member.return_value = member

        resp = await client.post(
            "/timeout_member",
            json={
                "guild_id": TEST_GUILD_ID,
                "user_id": 1000,
                "duration_seconds": 3600,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.CONFLICT
        body = await resp.json()
        assert body["error"] == "already_timed_out"
        assert "until" in body

    async def test_forbidden(self, client):
        member = make_mock_member()
        member.timeout.side_effect = _forbidden()
        client.mock_guild.fetch_member.return_value = member

        resp = await client.post(
            "/timeout_member",
            json={
                "guild_id": TEST_GUILD_ID,
                "user_id": 1000,
                "duration_seconds": 3600,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# /kick_member
# ---------------------------------------------------------------------------


class TestKickMember:
    async def test_success(self, client):
        member = make_mock_member()
        client.mock_guild.fetch_member.return_value = member

        resp = await client.post(
            "/kick_member",
            json={
                "guild_id": TEST_GUILD_ID,
                "user_id": 1000,
                "reason": "spam",
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["action"] == "kick_member"
        member.kick.assert_awaited_once_with(reason="spam")

    async def test_guild_not_allowed(self, client):
        resp = await client.post(
            "/kick_member",
            json={"guild_id": 999999, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_rate_limited(self, client):
        _fill_rate_limiter()
        resp = await client.post(
            "/kick_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.TOO_MANY_REQUESTS

    async def test_guild_not_found(self, client):
        client.mock_bot.get_guild.return_value = None
        resp = await client.post(
            "/kick_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND

    async def test_member_not_found(self, client):
        client.mock_guild.fetch_member.side_effect = _not_found()
        resp = await client.post(
            "/kick_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND

    async def test_target_is_moderator(self, client):
        member = make_mock_member(is_moderator=True)
        client.mock_guild.fetch_member.return_value = member

        resp = await client.post(
            "/kick_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY
        body = await resp.json()
        assert body["error"] == "target_is_moderator"

    async def test_member_too_old(self, client):
        member = make_mock_member(joined_at=datetime.now(UTC) - timedelta(days=8))
        client.mock_guild.fetch_member.return_value = member

        resp = await client.post(
            "/kick_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY
        body = await resp.json()
        assert body["error"] == "member_too_old"

    async def test_forbidden(self, client):
        member = make_mock_member()
        member.kick.side_effect = _forbidden()
        client.mock_guild.fetch_member.return_value = member

        resp = await client.post(
            "/kick_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# /ban_member
# ---------------------------------------------------------------------------


class TestBanMember:
    async def test_success(self, client):
        member = make_mock_member()
        client.mock_guild.fetch_member.return_value = member

        resp = await client.post(
            "/ban_member",
            json={
                "guild_id": TEST_GUILD_ID,
                "user_id": 1000,
                "reason": "bad actor",
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["action"] == "ban_member"
        member.ban.assert_awaited_once_with(reason="bad actor")

    async def test_guild_not_allowed(self, client):
        resp = await client.post(
            "/ban_member",
            json={"guild_id": 999999, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_rate_limited(self, client):
        _fill_rate_limiter()
        resp = await client.post(
            "/ban_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.TOO_MANY_REQUESTS

    async def test_guild_not_found(self, client):
        client.mock_bot.get_guild.return_value = None
        resp = await client.post(
            "/ban_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND

    async def test_member_not_found(self, client):
        client.mock_guild.fetch_member.side_effect = _not_found()
        resp = await client.post(
            "/ban_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND

    async def test_target_is_moderator(self, client):
        member = make_mock_member(is_moderator=True)
        client.mock_guild.fetch_member.return_value = member

        resp = await client.post(
            "/ban_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_member_too_old(self, client):
        member = make_mock_member(joined_at=datetime.now(UTC) - timedelta(days=8))
        client.mock_guild.fetch_member.return_value = member

        resp = await client.post(
            "/ban_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_forbidden(self, client):
        member = make_mock_member()
        member.ban.side_effect = _forbidden()
        client.mock_guild.fetch_member.return_value = member

        resp = await client.post(
            "/ban_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# /is_banned
# ---------------------------------------------------------------------------


class TestIsBanned:
    async def test_banned(self, client):
        ban_entry = MagicMock()
        ban_entry.reason = "spam"
        client.mock_guild.fetch_ban.return_value = ban_entry

        resp = await client.post(
            "/is_banned",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        body = await resp.json()
        assert body["banned"] is True
        assert body["reason"] == "spam"

    async def test_not_banned(self, client):
        client.mock_guild.fetch_ban.side_effect = _not_found()

        resp = await client.post(
            "/is_banned",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        body = await resp.json()
        assert body["banned"] is False

    async def test_guild_not_allowed(self, client):
        resp = await client.post(
            "/is_banned",
            json={"guild_id": 999999, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_guild_not_found(self, client):
        client.mock_bot.get_guild.return_value = None

        resp = await client.post(
            "/is_banned",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND

    async def test_forbidden(self, client):
        client.mock_guild.fetch_ban.side_effect = _forbidden()

        resp = await client.post(
            "/is_banned",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# /unlock_thread
# ---------------------------------------------------------------------------


def _track_lock(thread_id: int) -> None:
    """Pre-seed the action tracker so /unlock_thread accepts the request."""
    from sentinel.guardrails import action_tracker

    action_tracker.record(
        TEST_KEY_SECRET, "lock_thread", TEST_GUILD_ID, thread_id, ttl=3600
    )


def _track_timeout(user_id: int) -> None:
    from sentinel.guardrails import action_tracker

    action_tracker.record(TEST_KEY_SECRET, "timeout", TEST_GUILD_ID, user_id, ttl=3600)


def _track_ban(user_id: int) -> None:
    from sentinel.guardrails import action_tracker

    action_tracker.record(TEST_KEY_SECRET, "ban", TEST_GUILD_ID, user_id, ttl=3600)


@pytest.fixture(autouse=True)
def _reset_action_tracker():
    from sentinel.guardrails import action_tracker

    action_tracker._expiry.clear()
    yield
    action_tracker._expiry.clear()


class TestUnlockThread:
    async def test_success(self, client):
        thread = make_mock_thread()
        client.mock_guild.get_thread.return_value = thread
        _track_lock(thread.id)

        resp = await client.post(
            "/unlock_thread",
            json={
                "guild_id": TEST_GUILD_ID,
                "thread_id": thread.id,
                "reason": "appeal granted",
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["action"] == "unlock_thread"
        thread.edit.assert_awaited_once_with(
            locked=False, archived=False, reason="appeal granted"
        )

    async def test_guild_not_allowed(self, client):
        resp = await client.post(
            "/unlock_thread",
            json={"guild_id": 999999, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_not_sentinel_lock(self, client):
        # The tracker is empty — the lock wasn't applied by sentinel, so
        # unlock must refuse.
        resp = await client.post(
            "/unlock_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY
        body = await resp.json()
        assert body["error"] == "not_sentinel_lock"

    async def test_guild_not_found(self, client):
        _track_lock(3000)
        client.mock_bot.get_guild.return_value = None
        resp = await client.post(
            "/unlock_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND

    async def test_thread_not_found_via_fetch(self, client):
        _track_lock(3000)
        client.mock_guild.get_thread.return_value = None
        client.mock_guild.fetch_channel.side_effect = _not_found()
        resp = await client.post(
            "/unlock_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND
        body = await resp.json()
        assert body["error"] == "thread_not_found"

    async def test_fetched_channel_not_a_thread(self, client):
        # `fetch_channel` returns something that exists but isn't a Thread —
        # the handler must reject as "thread_not_found".
        _track_lock(3000)
        client.mock_guild.get_thread.return_value = None
        client.mock_guild.fetch_channel.return_value = MagicMock(
            spec=discord.TextChannel
        )
        resp = await client.post(
            "/unlock_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND
        body = await resp.json()
        assert body["error"] == "thread_not_found"

    async def test_thread_found_via_fetch(self, client):
        _track_lock(3000)
        thread = make_mock_thread()
        client.mock_guild.get_thread.return_value = None
        client.mock_guild.fetch_channel.return_value = thread

        resp = await client.post(
            "/unlock_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        thread.edit.assert_awaited_once()

    async def test_forbidden(self, client):
        thread = make_mock_thread()
        thread.edit.side_effect = _forbidden()
        client.mock_guild.get_thread.return_value = thread
        _track_lock(thread.id)

        resp = await client.post(
            "/unlock_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": thread.id},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.FORBIDDEN

    async def test_success_removes_tracker_entry(self, client):
        # After a successful unlock, the lock_thread tracker entry should
        # be cleared so a second unlock returns not_sentinel_lock.
        from sentinel.guardrails import action_tracker

        thread = make_mock_thread()
        client.mock_guild.get_thread.return_value = thread
        _track_lock(thread.id)

        resp = await client.post(
            "/unlock_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": thread.id},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        assert not action_tracker.is_tracked(
            TEST_KEY_SECRET, "lock_thread", TEST_GUILD_ID, thread.id
        )


# ---------------------------------------------------------------------------
# /remove_timeout
# ---------------------------------------------------------------------------


class TestRemoveTimeout:
    async def test_success(self, client):
        until = datetime.now(UTC) + timedelta(minutes=30)
        member = make_mock_member(is_timed_out=True, timed_out_until=until)
        client.mock_guild.fetch_member.return_value = member
        _track_timeout(member.id)

        resp = await client.post(
            "/remove_timeout",
            json={
                "guild_id": TEST_GUILD_ID,
                "user_id": member.id,
                "reason": "manual override",
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["action"] == "remove_timeout"
        # discord.py uses `member.timeout(None, reason=...)` to clear.
        member.timeout.assert_awaited_once()
        call = member.timeout.await_args
        assert call.args[0] is None
        assert call.kwargs.get("reason") == "manual override"

    async def test_guild_not_allowed(self, client):
        resp = await client.post(
            "/remove_timeout",
            json={"guild_id": 999999, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_not_sentinel_timeout(self, client):
        resp = await client.post(
            "/remove_timeout",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY
        body = await resp.json()
        assert body["error"] == "not_sentinel_timeout"

    async def test_guild_not_found(self, client):
        _track_timeout(1000)
        client.mock_bot.get_guild.return_value = None
        resp = await client.post(
            "/remove_timeout",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND

    async def test_member_not_found(self, client):
        _track_timeout(1000)
        client.mock_guild.fetch_member.side_effect = _not_found()
        resp = await client.post(
            "/remove_timeout",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND
        body = await resp.json()
        assert body["error"] == "member_not_found"

    async def test_not_timed_out_returns_conflict_and_clears_tracker(self, client):
        # Tracker says we timed them out, but member.is_timed_out() is False
        # — the timeout already expired. Server returns 409 and prunes the
        # tracker entry so future remove calls return not_sentinel_timeout.
        from sentinel.guardrails import action_tracker

        member = make_mock_member(is_timed_out=False)
        client.mock_guild.fetch_member.return_value = member
        _track_timeout(member.id)

        resp = await client.post(
            "/remove_timeout",
            json={"guild_id": TEST_GUILD_ID, "user_id": member.id},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.CONFLICT
        body = await resp.json()
        assert body["error"] == "not_timed_out"
        assert not action_tracker.is_tracked(
            TEST_KEY_SECRET, "timeout", TEST_GUILD_ID, member.id
        )

    async def test_forbidden(self, client):
        member = make_mock_member(is_timed_out=True)
        member.timeout.side_effect = _forbidden()
        client.mock_guild.fetch_member.return_value = member
        _track_timeout(member.id)

        resp = await client.post(
            "/remove_timeout",
            json={"guild_id": TEST_GUILD_ID, "user_id": member.id},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.FORBIDDEN

    async def test_success_clears_tracker(self, client):
        from sentinel.guardrails import action_tracker

        member = make_mock_member(is_timed_out=True)
        client.mock_guild.fetch_member.return_value = member
        _track_timeout(member.id)

        resp = await client.post(
            "/remove_timeout",
            json={"guild_id": TEST_GUILD_ID, "user_id": member.id},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        assert not action_tracker.is_tracked(
            TEST_KEY_SECRET, "timeout", TEST_GUILD_ID, member.id
        )


# ---------------------------------------------------------------------------
# /unban_member
# ---------------------------------------------------------------------------


class TestUnbanMember:
    async def test_success(self, client):
        _track_ban(1000)
        client.mock_guild.unban = MagicMock()
        # discord.py exposes unban as an async method on Guild.
        from unittest.mock import AsyncMock

        client.mock_guild.unban = AsyncMock()

        resp = await client.post(
            "/unban_member",
            json={
                "guild_id": TEST_GUILD_ID,
                "user_id": 1000,
                "reason": "appeal granted",
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["action"] == "unban_member"
        # Verify unban was called with a discord.Object(user_id) and reason.
        client.mock_guild.unban.assert_awaited_once()
        args, kwargs = client.mock_guild.unban.call_args
        assert isinstance(args[0], discord.Object)
        assert args[0].id == 1000
        assert kwargs.get("reason") == "appeal granted"

    async def test_guild_not_allowed(self, client):
        resp = await client.post(
            "/unban_member",
            json={"guild_id": 999999, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_not_sentinel_ban(self, client):
        resp = await client.post(
            "/unban_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY
        body = await resp.json()
        assert body["error"] == "not_sentinel_ban"

    async def test_guild_not_found(self, client):
        _track_ban(1000)
        client.mock_bot.get_guild.return_value = None
        resp = await client.post(
            "/unban_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND

    async def test_ban_not_found(self, client):
        from unittest.mock import AsyncMock

        _track_ban(1000)
        client.mock_guild.unban = AsyncMock(side_effect=_not_found())

        resp = await client.post(
            "/unban_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND
        body = await resp.json()
        assert body["error"] == "ban_not_found"

    async def test_forbidden(self, client):
        from unittest.mock import AsyncMock

        _track_ban(1000)
        client.mock_guild.unban = AsyncMock(side_effect=_forbidden())

        resp = await client.post(
            "/unban_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.FORBIDDEN

    async def test_success_clears_tracker(self, client):
        from unittest.mock import AsyncMock

        from sentinel.guardrails import action_tracker

        _track_ban(1000)
        client.mock_guild.unban = AsyncMock()

        resp = await client.post(
            "/unban_member",
            json={"guild_id": TEST_GUILD_ID, "user_id": 1000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        assert not action_tracker.is_tracked(
            TEST_KEY_SECRET, "ban", TEST_GUILD_ID, 1000
        )


# ---------------------------------------------------------------------------
# Scattered gaps in already-covered handlers
# ---------------------------------------------------------------------------


class TestDeleteMessageEdgeCases:
    async def test_fetched_channel_not_messageable(self, client):
        # `fetch_channel` returns something existing but not Messageable
        # (e.g. a category). The handler must report channel_not_found.
        client.mock_guild.get_channel.return_value = None
        client.mock_guild.get_thread.return_value = None
        # Plain MagicMock (no spec) is not an instance of `discord.abc.Messageable`.
        client.mock_guild.fetch_channel.return_value = MagicMock()

        resp = await client.post(
            "/delete_message",
            json={
                "guild_id": TEST_GUILD_ID,
                "channel_id": 4000,
                "message_id": 2000,
            },
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND
        body = await resp.json()
        assert body["error"] == "channel_not_found"


class TestLockThreadEdgeCases:
    async def test_fetched_channel_not_a_thread(self, client):
        # `fetch_channel` returns a non-Thread (e.g. a text channel).
        client.mock_guild.get_thread.return_value = None
        client.mock_guild.fetch_channel.return_value = MagicMock(
            spec=discord.TextChannel
        )
        resp = await client.post(
            "/lock_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND
        body = await resp.json()
        assert body["error"] == "thread_not_found"


class TestDeleteThreadEdgeCases:
    async def test_guild_not_allowed(self, client):
        resp = await client.post(
            "/delete_thread",
            json={"guild_id": 999999, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY

    async def test_rate_limited(self, client):
        _fill_rate_limiter()
        resp = await client.post(
            "/delete_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.TOO_MANY_REQUESTS

    async def test_guild_not_found(self, client):
        client.mock_bot.get_guild.return_value = None
        resp = await client.post(
            "/delete_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND

    async def test_fetched_channel_not_a_thread(self, client):
        client.mock_guild.get_thread.return_value = None
        client.mock_guild.fetch_channel.return_value = MagicMock(
            spec=discord.TextChannel
        )
        resp = await client.post(
            "/delete_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.NOT_FOUND
        body = await resp.json()
        assert body["error"] == "thread_not_found"

    async def test_thread_found_via_fetch(self, client):
        # `get_thread` misses, but `fetch_channel` returns a real Thread —
        # the handler should accept and proceed to delete.
        client.mock_guild.get_thread.return_value = None
        thread = make_mock_thread()
        client.mock_guild.fetch_channel.return_value = thread

        resp = await client.post(
            "/delete_thread",
            json={"guild_id": TEST_GUILD_ID, "thread_id": 3000},
            headers=_headers(),
        )
        assert resp.status == HTTPStatus.OK
        thread.delete.assert_awaited_once()
