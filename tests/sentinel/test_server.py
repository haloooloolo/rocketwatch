import time
from collections import deque
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from unittest.mock import MagicMock

import discord
from guardrails import rate_limiter
from helpers import (
    TEST_GUILD_ID,
    TEST_KEY_SECRET,
    make_mock_channel,
    make_mock_member,
    make_mock_message,
    make_mock_thread,
)


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
