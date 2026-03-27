import time
from collections import defaultdict, deque
from datetime import UTC, datetime

import discord

from config import KeyConfig


class RateLimiter:
    def __init__(self) -> None:
        self._timestamps: dict[str, deque[float]] = defaultdict(deque)
        self._window = 3600.0

    def check(self, key: KeyConfig) -> int | None:
        """Return None if allowed, or seconds until next slot if rate limited."""
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


def check_guild(key: KeyConfig, guild_id: int) -> str | None:
    if guild_id not in key.allowed_server_ids:
        return "guild_not_allowed"
    return None


def check_message_age(
    limit: int, message: discord.Message
) -> tuple[str, int, int] | None:
    if limit <= 0:
        return "action_disabled", 0, 403
    age = (datetime.now(UTC) - message.created_at).total_seconds()
    if age > limit:
        return "message_too_old", int(age), 422
    return None


def check_thread_age(limit: int, thread: discord.Thread) -> tuple[str, int, int] | None:
    if limit <= 0:
        return "action_disabled", 0, 403
    if thread.created_at is None:
        return "thread_age_unknown", 0, 422
    age = (datetime.now(UTC) - thread.created_at).total_seconds()
    if age > limit:
        return "thread_too_old", int(age), 422
    return None


def check_timeout_duration(
    key: KeyConfig, duration_seconds: int
) -> tuple[str, int] | None:
    if key.timeout_member_max_duration <= 0:
        return "action_disabled", 403
    if duration_seconds < 1:
        return "duration_too_short", 422
    if duration_seconds > key.timeout_member_max_duration:
        return "duration_exceeds_limit", 422
    return None


def check_member_age(limit: int, member: discord.Member) -> tuple[str, int, int] | None:
    if limit <= 0:
        return "action_disabled", 0, 403
    if member.joined_at is None:
        return "member_age_unknown", 0, 422
    age = (datetime.now(UTC) - member.joined_at).total_seconds()
    if age > limit:
        return "member_too_old", int(age), 422
    return None


def check_moderator(member: discord.Member) -> bool:
    return member.guild_permissions.moderate_members
