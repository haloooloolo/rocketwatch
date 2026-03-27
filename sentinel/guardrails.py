import time
from collections import defaultdict, deque
from datetime import UTC, datetime

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
    key: KeyConfig, created_at: datetime
) -> tuple[str, int, int] | None:
    if key.max_message_age_seconds <= 0:
        return "action_disabled", 0, 403
    age = (datetime.now(UTC) - created_at).total_seconds()
    if age > key.max_message_age_seconds:
        return "message_too_old", int(age), 422
    return None


def check_thread_age(
    key: KeyConfig, created_at: datetime
) -> tuple[str, int, int] | None:
    if key.max_thread_age_seconds <= 0:
        return "action_disabled", 0, 403
    age = (datetime.now(UTC) - created_at).total_seconds()
    if age > key.max_thread_age_seconds:
        return "thread_too_old", int(age), 422
    return None


def check_timeout_duration(
    key: KeyConfig, duration_seconds: int
) -> tuple[str, int] | None:
    if key.max_timeout_seconds <= 0:
        return "action_disabled", 403
    if duration_seconds < 1:
        return "duration_too_short", 422
    if duration_seconds > key.max_timeout_seconds:
        return "duration_exceeds_limit", 422
    return None


def check_moderator(member) -> bool:
    return member.guild_permissions.moderate_members
