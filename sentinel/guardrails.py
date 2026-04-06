import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from http import HTTPStatus

import discord
from sentinel.config import KeyConfig


class RateLimiter:
    def __init__(self) -> None:
        self._timestamps: dict[str, deque[float]] = defaultdict(deque)
        self._window = 3600.0

    def check(self, key: KeyConfig) -> int | None:
        """Return None if allowed, or seconds until next slot if rate limited."""
        if key.max_actions_per_hour is None:
            return None

        now = time.monotonic()
        cutoff = now - self._window
        timestamps = self._timestamps[key.secret]
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

        if len(timestamps) >= key.max_actions_per_hour:
            return int(timestamps[0] - cutoff) + 1

        timestamps.append(now)
        return None


rate_limiter = RateLimiter()


class ActionTracker:
    """TTL cache tracking sentinel-applied bans and timeouts."""

    def __init__(self) -> None:
        self._expiry: dict[str, float] = {}

    def _tag(self, key_secret: str, action: str, guild_id: int, user_id: int) -> str:
        return f"{key_secret}:{action}:{guild_id}:{user_id}"

    def _prune(self) -> None:
        now = time.monotonic()
        self._expiry = {k: v for k, v in self._expiry.items() if v > now}

    def record(
        self, key_secret: str, action: str, guild_id: int, user_id: int, ttl: float
    ) -> None:
        self._expiry[self._tag(key_secret, action, guild_id, user_id)] = (
            time.monotonic() + ttl
        )

    def is_tracked(
        self, key_secret: str, action: str, guild_id: int, user_id: int
    ) -> bool:
        self._prune()
        return self._tag(key_secret, action, guild_id, user_id) in self._expiry

    def remove(self, key_secret: str, action: str, guild_id: int, user_id: int) -> None:
        self._expiry.pop(self._tag(key_secret, action, guild_id, user_id), None)


action_tracker = ActionTracker()


def check_guild(key: KeyConfig, guild_id: int) -> str | None:
    if guild_id not in key.allowed_server_ids:
        return "guild_not_allowed"
    return None


def check_message_age(
    limit: int | None, message: discord.Message
) -> tuple[str, int, int] | None:
    if limit is None:
        return None
    if limit is not None and limit <= 0:
        return "action_disabled", 0, HTTPStatus.FORBIDDEN
    age = (datetime.now(UTC) - message.created_at).total_seconds()
    if age > limit:
        return "message_too_old", int(age), HTTPStatus.UNPROCESSABLE_ENTITY
    return None


def check_thread_age(
    limit: int | None, thread: discord.Thread
) -> tuple[str, int, int] | None:
    if limit is None:
        return None
    if limit <= 0:
        return "action_disabled", 0, HTTPStatus.FORBIDDEN
    if thread.created_at is None:
        return "thread_age_unknown", 0, HTTPStatus.UNPROCESSABLE_ENTITY
    age = (datetime.now(UTC) - thread.created_at).total_seconds()
    if age > limit:
        return "thread_too_old", int(age), HTTPStatus.UNPROCESSABLE_ENTITY
    return None


def check_timeout_duration(
    key: KeyConfig, duration_seconds: int
) -> tuple[str, int] | None:
    if duration_seconds < 1:
        return "duration_too_short", HTTPStatus.UNPROCESSABLE_ENTITY
    if key.timeout_member_max_duration is None:
        return None
    if key.timeout_member_max_duration <= 0:
        return "action_disabled", HTTPStatus.FORBIDDEN
    if duration_seconds > key.timeout_member_max_duration:
        return "duration_exceeds_limit", HTTPStatus.UNPROCESSABLE_ENTITY
    return None


def check_member_age(
    limit: int | None, member: discord.Member
) -> tuple[str, int, int] | None:
    if limit is None:
        return None
    if limit <= 0:
        return "action_disabled", 0, HTTPStatus.FORBIDDEN
    if member.joined_at is None:
        return "member_age_unknown", 0, HTTPStatus.UNPROCESSABLE_ENTITY
    age = (datetime.now(UTC) - member.joined_at).total_seconds()
    if age > limit:
        return "member_too_old", int(age), HTTPStatus.UNPROCESSABLE_ENTITY
    return None


def check_moderator(member: discord.Member) -> bool:
    return member.guild_permissions.moderate_members
