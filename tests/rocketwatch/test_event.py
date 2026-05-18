from datetime import datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_typing import BlockNumber

from rocketwatch.utils import event as event_module
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.event import Event, EventPlugin


def _make_event(
    *, block_number: int, transaction_index: int = 0, event_index: int = 0
) -> Event:
    return Event(
        embed=Embed(),
        topic="topic",
        event_name="event_name",
        unique_id=f"id-{block_number}-{transaction_index}-{event_index}",
        block_number=cast(BlockNumber, block_number),
        transaction_index=transaction_index,
        event_index=event_index,
    )


class TestEventGetScore:
    def test_score_orders_by_block_then_tx_then_event(self):
        # Three events with strictly-increasing positions should yield
        # strictly-increasing scores so sort() lines them up correctly.
        events = [
            _make_event(block_number=1, transaction_index=0, event_index=0),
            _make_event(block_number=1, transaction_index=0, event_index=1),
            _make_event(block_number=1, transaction_index=1, event_index=0),
            _make_event(block_number=2, transaction_index=0, event_index=0),
        ]
        scores = [e.get_score() for e in events]
        assert scores == sorted(scores)
        # Strict monotonic: no ties.
        assert len(set(scores)) == len(scores)

    def test_block_dominates_lower_dimensions(self):
        # A later block beats arbitrarily-large tx_index / event_index on
        # the previous block.
        earlier = _make_event(block_number=1, transaction_index=999, event_index=999)
        later = _make_event(block_number=2, transaction_index=0, event_index=0)
        assert later.get_score() > earlier.get_score()

    def test_event_is_frozen(self):
        # Event is `dataclass(frozen=True, slots=True)`; mutation must raise
        # so callers can't accidentally change a logged event in-place.
        ev = _make_event(block_number=1)
        with pytest.raises(AttributeError):
            ev.block_number = cast(BlockNumber, 99)  # type: ignore[misc]


class _ConcretePlugin(EventPlugin):
    """Minimal concrete subclass for testing the EventPlugin base."""

    def __init__(self, bot, rate_limit: timedelta = timedelta(seconds=5)) -> None:
        super().__init__(bot, rate_limit=rate_limit)
        self.calls = 0

    async def _get_new_events(self):
        self.calls += 1
        return []


class TestEventPluginInit:
    def test_initialises_from_cfg(self, monkeypatch: pytest.MonkeyPatch):
        # The base reads `cfg.events.lookback_distance` and `cfg.events.genesis`
        # at construction time. With our baseline cfg those are 10 and 0.
        plugin = _ConcretePlugin(MagicMock())
        assert plugin.lookback_distance == 10
        # genesis=0 → last_served_block should be -1 (genesis - 1).
        assert plugin.last_served_block == -1

    def test_start_tracking_anchors_one_below_block(self):
        plugin = _ConcretePlugin(MagicMock())
        plugin.start_tracking(cast(BlockNumber, 1000))
        assert plugin.last_served_block == 999


class TestEventPluginRateLimit:
    async def test_first_call_runs_underlying_hook(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # On the first call (after rate-limit has elapsed via the init-time
        # back-date), the hook runs and last_served_block advances.
        monkeypatch.setattr(event_module.w3, "eth", type("E", (), {})(), raising=False)
        event_module.w3.eth.get_block_number = AsyncMock(return_value=42)

        plugin = _ConcretePlugin(MagicMock())
        await plugin.get_new_events()
        assert plugin.calls == 1
        assert plugin.last_served_block == 42

    async def test_second_call_inside_window_is_throttled(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Two rapid calls — the second must NOT trigger `_get_new_events`.
        monkeypatch.setattr(event_module.w3, "eth", type("E", (), {})(), raising=False)
        event_module.w3.eth.get_block_number = AsyncMock(return_value=42)

        plugin = _ConcretePlugin(MagicMock(), rate_limit=timedelta(seconds=10))
        await plugin.get_new_events()
        # Immediately call again — within rate_limit window.
        result = await plugin.get_new_events()
        assert result == []
        assert plugin.calls == 1

    async def test_window_advance_resets_throttle(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # By rewinding `_last_run` we simulate the rate-limit window elapsing.
        monkeypatch.setattr(event_module.w3, "eth", type("E", (), {})(), raising=False)
        event_module.w3.eth.get_block_number = AsyncMock(return_value=100)

        plugin = _ConcretePlugin(MagicMock(), rate_limit=timedelta(seconds=10))
        await plugin.get_new_events()
        plugin._last_run = datetime.now() - timedelta(seconds=60)
        await plugin.get_new_events()
        assert plugin.calls == 2


class TestEventPluginDefaults:
    async def test_get_past_events_returns_empty_by_default(self):
        # Subclasses are expected to override; the base returns no events so
        # plugins without backfill don't crash on a replay request.
        plugin = _ConcretePlugin(MagicMock())
        assert (
            await plugin.get_past_events(cast(BlockNumber, 0), cast(BlockNumber, 100))
            == []
        )
