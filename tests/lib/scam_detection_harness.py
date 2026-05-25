"""Test harness for the scam_detection report pipeline: a scripted Sentinel
double plus a ReportContext builder."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

from rocketwatch.plugins.scam_detection.common import ReportContext
from tests.lib.discord_harness import make_bot


class ScriptedSentinel:
    """A SentinelClient stand-in. All action methods are AsyncMocks defaulting
    to True (is_banned to False); tests override `.return_value` as needed."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.delete_message = AsyncMock(return_value=True)
        self.timeout_member = AsyncMock(return_value=True)
        self.lock_thread = AsyncMock(return_value=True)
        self.ban_member = AsyncMock(return_value=True)
        self.remove_timeout = AsyncMock(return_value=True)
        self.unlock_thread = AsyncMock(return_value=True)
        self.is_banned = AsyncMock(return_value=False)


def make_ctx(
    *, bot: Any = None, db: Any = None, sentinel: ScriptedSentinel | None = None
) -> ReportContext:
    # Accept a pre-built (MagicMock) bot so tests can configure/assert on it via
    # an Any-typed handle rather than through the typed `ctx.bot`.
    return ReportContext(
        bot=cast(Any, bot if bot is not None else make_bot(db=db)),
        sentinel=cast(Any, sentinel or ScriptedSentinel()),
    )
