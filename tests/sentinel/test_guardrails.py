import time
from collections import deque
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from unittest.mock import MagicMock

from config import KeyConfig
from guardrails import (
    check_guild,
    check_member_age,
    check_message_age,
    check_moderator,
    check_thread_age,
    check_timeout_duration,
    rate_limiter,
)


class TestCheckGuild:
    def test_allowed_guild(self):
        key = KeyConfig(secret="s", allowed_server_ids=[1, 2])
        assert check_guild(key, 1) is None

    def test_disallowed_guild(self):
        key = KeyConfig(secret="s", allowed_server_ids=[1])
        assert check_guild(key, 2) == "guild_not_allowed"

    def test_empty_allowed_list_rejects_all(self):
        key = KeyConfig(secret="s", allowed_server_ids=[])
        assert check_guild(key, 1) == "guild_not_allowed"


class TestCheckMessageAge:
    def test_young_message_passes(self):
        msg = MagicMock()
        msg.created_at = datetime.now(UTC) - timedelta(seconds=60)
        assert check_message_age(900, msg) is None

    def test_old_message_fails(self):
        msg = MagicMock()
        msg.created_at = datetime.now(UTC) - timedelta(seconds=1000)
        result = check_message_age(900, msg)
        assert result is not None
        error, age, status = result
        assert error == "message_too_old"
        assert age >= 1000
        assert status == HTTPStatus.UNPROCESSABLE_ENTITY

    def test_zero_limit_returns_action_disabled(self):
        msg = MagicMock()
        msg.created_at = datetime.now(UTC)
        assert check_message_age(0, msg) == ("action_disabled", 0, HTTPStatus.FORBIDDEN)

    def test_negative_limit_returns_action_disabled(self):
        msg = MagicMock()
        msg.created_at = datetime.now(UTC)
        assert check_message_age(-1, msg) == (
            "action_disabled",
            0,
            HTTPStatus.FORBIDDEN,
        )

    def test_none_limit_allows_any_age(self):
        msg = MagicMock()
        msg.created_at = datetime.now(UTC) - timedelta(days=365)
        assert check_message_age(None, msg) is None


class TestCheckThreadAge:
    def test_young_thread_passes(self):
        thread = MagicMock()
        thread.created_at = datetime.now(UTC) - timedelta(seconds=60)
        assert check_thread_age(3600, thread) is None

    def test_old_thread_fails(self):
        thread = MagicMock()
        thread.created_at = datetime.now(UTC) - timedelta(seconds=4000)
        result = check_thread_age(3600, thread)
        assert result is not None
        error, age, status = result
        assert error == "thread_too_old"
        assert age >= 4000
        assert status == HTTPStatus.UNPROCESSABLE_ENTITY

    def test_zero_limit_returns_action_disabled(self):
        thread = MagicMock()
        assert check_thread_age(0, thread) == (
            "action_disabled",
            0,
            HTTPStatus.FORBIDDEN,
        )

    def test_none_created_at(self):
        thread = MagicMock()
        thread.created_at = None
        assert check_thread_age(3600, thread) == (
            "thread_age_unknown",
            0,
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    def test_none_limit_allows_any_age(self):
        thread = MagicMock()
        thread.created_at = datetime.now(UTC) - timedelta(days=365)
        assert check_thread_age(None, thread) is None


class TestCheckTimeoutDuration:
    def test_valid_duration(self):
        key = KeyConfig(secret="s", timeout_member_max_duration=86400)
        assert check_timeout_duration(key, 3600) is None

    def test_exceeding_duration(self):
        key = KeyConfig(secret="s", timeout_member_max_duration=86400)
        assert check_timeout_duration(key, 90000) == (
            "duration_exceeds_limit",
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    def test_exact_boundary_passes(self):
        key = KeyConfig(secret="s", timeout_member_max_duration=86400)
        assert check_timeout_duration(key, 86400) is None

    def test_zero_duration_rejected(self):
        key = KeyConfig(secret="s", timeout_member_max_duration=86400)
        assert check_timeout_duration(key, 0) == (
            "duration_too_short",
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    def test_negative_duration_rejected(self):
        key = KeyConfig(secret="s", timeout_member_max_duration=86400)
        assert check_timeout_duration(key, -5) == (
            "duration_too_short",
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    def test_zero_max_returns_action_disabled(self):
        key = KeyConfig(secret="s", timeout_member_max_duration=0)
        assert check_timeout_duration(key, 3600) == (
            "action_disabled",
            HTTPStatus.FORBIDDEN,
        )

    def test_negative_max_returns_action_disabled(self):
        key = KeyConfig(secret="s", timeout_member_max_duration=-1)
        assert check_timeout_duration(key, 3600) == (
            "action_disabled",
            HTTPStatus.FORBIDDEN,
        )

    def test_none_max_allows_any_duration(self):
        key = KeyConfig(secret="s", timeout_member_max_duration=None)
        assert check_timeout_duration(key, 999999) is None

    def test_none_max_still_rejects_zero_duration(self):
        key = KeyConfig(secret="s", timeout_member_max_duration=None)
        assert check_timeout_duration(key, 0) == (
            "duration_too_short",
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )


class TestCheckMemberAge:
    def test_recent_member_passes(self):
        member = MagicMock()
        member.joined_at = datetime.now(UTC) - timedelta(hours=1)
        assert check_member_age(604800, member) is None

    def test_old_member_fails(self):
        member = MagicMock()
        member.joined_at = datetime.now(UTC) - timedelta(days=8)
        result = check_member_age(604800, member)
        assert result is not None
        error, age, status = result
        assert error == "member_too_old"
        assert age >= 604800
        assert status == HTTPStatus.UNPROCESSABLE_ENTITY

    def test_zero_limit_returns_action_disabled(self):
        member = MagicMock()
        assert check_member_age(0, member) == (
            "action_disabled",
            0,
            HTTPStatus.FORBIDDEN,
        )

    def test_none_joined_at(self):
        member = MagicMock()
        member.joined_at = None
        assert check_member_age(604800, member) == (
            "member_age_unknown",
            0,
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    def test_none_limit_allows_any_age(self):
        member = MagicMock()
        member.joined_at = datetime.now(UTC) - timedelta(days=365)
        assert check_member_age(None, member) is None


class TestCheckModerator:
    def test_moderator_returns_true(self):
        member = MagicMock()
        member.guild_permissions.moderate_members = True
        assert check_moderator(member) is True

    def test_non_moderator_returns_false(self):
        member = MagicMock()
        member.guild_permissions.moderate_members = False
        assert check_moderator(member) is False


class TestRateLimiter:
    def test_first_action_allowed(self):
        key = KeyConfig(secret="s", max_actions_per_hour=100)
        assert rate_limiter.check(key) is None

    def test_within_limit_allowed(self):
        key = KeyConfig(secret="s", max_actions_per_hour=5)
        for _ in range(4):
            assert rate_limiter.check(key) is None
        assert rate_limiter.check(key) is None

    def test_exceeding_limit_returns_retry_after(self):
        key = KeyConfig(secret="s", max_actions_per_hour=3)
        for _ in range(3):
            rate_limiter.check(key)
        result = rate_limiter.check(key)
        assert result is not None
        assert isinstance(result, int)
        assert result > 0

    def test_different_keys_independent(self):
        key_a = KeyConfig(secret="a", max_actions_per_hour=1)
        key_b = KeyConfig(secret="b", max_actions_per_hour=1)
        assert rate_limiter.check(key_a) is None
        assert rate_limiter.check(key_a) is not None
        assert rate_limiter.check(key_b) is None

    def test_none_limit_allows_unlimited_actions(self):
        key = KeyConfig(secret="unlimited", max_actions_per_hour=None)
        for _ in range(200):
            assert rate_limiter.check(key) is None

    def test_window_expiry_allows_new_actions(self):
        key = KeyConfig(secret="s", max_actions_per_hour=1)
        assert rate_limiter.check(key) is None
        assert rate_limiter.check(key) is not None
        # Simulate the timestamp being older than the 1-hour window
        now = time.monotonic()
        rate_limiter._timestamps[key.secret] = deque([now - 3601])
        assert rate_limiter.check(key) is None
