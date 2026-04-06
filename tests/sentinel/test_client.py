from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from helpers import (
    TEST_GUILD_ID,
    TEST_KEY_SECRET,
    make_mock_channel,
    make_mock_member,
    make_mock_message,
    make_mock_thread,
)

from rocketwatch.utils.config import cfg as rw_cfg
from rocketwatch.utils.sentinel import SentinelClient
from sentinel.server import create_app


def _make_discord_message(guild_id=TEST_GUILD_ID, channel_id=4000, message_id=2000):
    msg = MagicMock()
    msg.guild.id = guild_id
    msg.channel.id = channel_id
    msg.id = message_id
    return msg


def _make_discord_thread(guild_id=TEST_GUILD_ID, thread_id=3000):
    thread = MagicMock()
    thread.guild.id = guild_id
    thread.id = thread_id
    return thread


def _make_discord_member(guild_id=TEST_GUILD_ID, user_id=1000):
    member = MagicMock()
    member.guild.id = guild_id
    member.id = user_id
    return member


@pytest.fixture
async def sentinel_client():
    """Start a real sentinel test server and return a SentinelClient pointed at it."""
    guild = MagicMock()
    guild.id = TEST_GUILD_ID
    guild.get_channel = MagicMock(return_value=None)
    guild.get_thread = MagicMock(return_value=None)
    guild.fetch_channel = AsyncMock()
    guild.fetch_member = AsyncMock()
    guild.fetch_ban = AsyncMock()

    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)

    app = create_app(bot)
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()

    base_url = str(server.make_url(""))

    # Inject a mock rocketwatch config with sentinel URL/key pointing at test server
    rw_mock = MagicMock()
    rw_mock.sentinel.api_url = base_url
    rw_mock.sentinel.api_key = TEST_KEY_SECRET
    rw_cfg._instance = rw_mock

    client = SentinelClient()
    client.mock_guild = guild
    yield client

    await client.close()
    await test_client.close()
    rw_cfg._instance = None


class TestDeleteMessage:
    async def test_success(self, sentinel_client):
        channel = make_mock_channel()
        message = make_mock_message()
        channel.fetch_message.return_value = message
        sentinel_client.mock_guild.get_channel.return_value = channel

        msg = _make_discord_message()
        result = await sentinel_client.delete_message(msg, "spam")
        assert result is True
        message.delete.assert_awaited_once()

    async def test_error_returns_false(self, sentinel_client):
        msg = _make_discord_message(guild_id=999999)
        result = await sentinel_client.delete_message(msg, "spam")
        assert result is False

    async def test_guild_none_returns_false(self, sentinel_client):
        msg = MagicMock()
        msg.guild = None
        result = await sentinel_client.delete_message(msg, "spam")
        assert result is False


class TestLockThread:
    async def test_success(self, sentinel_client):
        thread = make_mock_thread()
        sentinel_client.mock_guild.get_thread.return_value = thread

        t = _make_discord_thread()
        result = await sentinel_client.lock_thread(t, "resolved")
        assert result is True
        thread.edit.assert_awaited_once()

    async def test_guild_none_returns_false(self, sentinel_client):
        t = MagicMock()
        t.guild = None
        result = await sentinel_client.lock_thread(t, "reason")
        assert result is False


class TestDeleteThread:
    async def test_success(self, sentinel_client):
        thread = make_mock_thread()
        sentinel_client.mock_guild.get_thread.return_value = thread

        t = _make_discord_thread()
        result = await sentinel_client.delete_thread(t, "spam")
        assert result is True
        thread.delete.assert_awaited_once()

    async def test_guild_none_returns_false(self, sentinel_client):
        t = MagicMock()
        t.guild = None
        result = await sentinel_client.delete_thread(t, "reason")
        assert result is False


class TestTimeoutMember:
    async def test_success(self, sentinel_client):
        member = make_mock_member()
        sentinel_client.mock_guild.fetch_member.return_value = member

        m = _make_discord_member()
        result = await sentinel_client.timeout_member(m, 3600, "spam")
        assert result is True
        member.timeout.assert_awaited_once()

    async def test_error_returns_false(self, sentinel_client):
        m = _make_discord_member(guild_id=999999)
        result = await sentinel_client.timeout_member(m, 3600, "spam")
        assert result is False


class TestKickMember:
    async def test_success(self, sentinel_client):
        member = make_mock_member()
        sentinel_client.mock_guild.fetch_member.return_value = member

        m = _make_discord_member()
        result = await sentinel_client.kick_member(m, "spam")
        assert result is True
        member.kick.assert_awaited_once()

    async def test_error_returns_false(self, sentinel_client):
        m = _make_discord_member(guild_id=999999)
        result = await sentinel_client.kick_member(m, "spam")
        assert result is False


class TestBanMember:
    async def test_success(self, sentinel_client):
        member = make_mock_member()
        sentinel_client.mock_guild.fetch_member.return_value = member

        m = _make_discord_member()
        result = await sentinel_client.ban_member(m, "bad actor")
        assert result is True
        member.ban.assert_awaited_once()

    async def test_error_returns_false(self, sentinel_client):
        m = _make_discord_member(guild_id=999999)
        result = await sentinel_client.ban_member(m, "bad actor")
        assert result is False


class TestIsBanned:
    async def test_banned(self, sentinel_client):
        ban_entry = MagicMock()
        ban_entry.reason = "spam"
        sentinel_client.mock_guild.fetch_ban.return_value = ban_entry

        result = await sentinel_client.is_banned(TEST_GUILD_ID, 1000)
        assert result is True

    async def test_not_banned(self, sentinel_client):
        from discord import NotFound

        resp = MagicMock(status=404)
        sentinel_client.mock_guild.fetch_ban.side_effect = NotFound(resp, "Not Found")

        result = await sentinel_client.is_banned(TEST_GUILD_ID, 1000)
        assert result is False

    async def test_guild_not_allowed(self, sentinel_client):
        result = await sentinel_client.is_banned(999999, 1000)
        assert result is None


class TestIsTimedOut:
    async def test_timed_out(self, sentinel_client):
        member = make_mock_member(is_timed_out=True)
        sentinel_client.mock_guild.fetch_member.return_value = member

        result = await sentinel_client.is_timed_out(TEST_GUILD_ID, 1000)
        assert result is True

    async def test_not_timed_out(self, sentinel_client):
        member = make_mock_member()
        sentinel_client.mock_guild.fetch_member.return_value = member

        result = await sentinel_client.is_timed_out(TEST_GUILD_ID, 1000)
        assert result is False

    async def test_member_not_found(self, sentinel_client):
        from discord import NotFound

        resp = MagicMock(status=404)
        sentinel_client.mock_guild.fetch_member.side_effect = NotFound(
            resp, "Not Found"
        )

        result = await sentinel_client.is_timed_out(TEST_GUILD_ID, 9999)
        assert result is None


class TestNonJsonErrorResponse:
    """Sentinel may return non-JSON errors (e.g. plain text 500 from a proxy)."""

    @pytest.fixture
    async def plaintext_500_client(self):
        async def handle(request):
            return web.Response(text="Internal Server Error", status=500)

        app = web.Application()
        for endpoint in [
            "/delete_message",
            "/lock_thread",
            "/delete_thread",
            "/timeout_member",
            "/kick_member",
            "/ban_member",
        ]:
            app.router.add_post(endpoint, handle)

        server = TestServer(app)
        test_client = TestClient(server)
        await test_client.start_server()

        rw_mock = MagicMock()
        rw_mock.sentinel.api_url = str(server.make_url(""))
        rw_mock.sentinel.api_key = TEST_KEY_SECRET
        rw_cfg._instance = rw_mock

        client = SentinelClient()
        yield client

        await client.close()
        await test_client.close()
        rw_cfg._instance = None

    async def test_delete_message(self, plaintext_500_client):
        msg = _make_discord_message()
        result = await plaintext_500_client.delete_message(msg, "spam")
        assert result is False

    async def test_lock_thread(self, plaintext_500_client):
        t = _make_discord_thread()
        result = await plaintext_500_client.lock_thread(t, "spam")
        assert result is False

    async def test_timeout_member(self, plaintext_500_client):
        m = _make_discord_member()
        result = await plaintext_500_client.timeout_member(m, 3600, "spam")
        assert result is False


class TestSessionManagement:
    async def test_close(self, sentinel_client):
        # Make a real request to create the session
        m = _make_discord_member(guild_id=999999)
        await sentinel_client.timeout_member(m, 3600, "test")
        assert sentinel_client._session is not None
        await sentinel_client.close()
        assert sentinel_client._session.closed
