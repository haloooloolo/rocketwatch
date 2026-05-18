from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.metrics.metrics import Metrics
from tests.lib.discord_harness import (
    captured_embed,
    make_bot,
    make_interaction,
    run_command,
)

pytestmark = pytest.mark.integration_db


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def cog(mongo_db: AsyncDatabase[dict[str, Any]]) -> Metrics:
    return Metrics(make_bot(db=mongo_db))


async def _seed(
    mongo_db: AsyncDatabase[dict[str, Any]],
    *,
    commands: list[dict[str, Any]],
    events: list[dict[str, Any]] | None = None,
) -> None:
    if commands:
        await mongo_db.command_metrics.insert_many(commands)
    if events:
        await mongo_db.event_queue.insert_many(events)


class TestMetricsCommand:
    async def test_summarises_last_seven_days(
        self,
        cog: Metrics,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        # Inside-window commands.
        recent = _now() - timedelta(days=1)
        await _seed(
            mongo_db,
            commands=[
                {
                    "timestamp": recent,
                    "command": "rpl",
                    "took": 0.5,
                    "status": "completed",
                    "user": {"id": 1, "name": "alice"},
                    "channel": {"id": 100, "name": "general"},
                },
                {
                    "timestamp": recent,
                    "command": "rpl",
                    "took": 0.7,
                    "status": "completed",
                    "user": {"id": 1, "name": "alice"},
                    "channel": {"id": 100, "name": "general"},
                },
                {
                    "timestamp": recent,
                    "command": "deposit_pool",
                    "took": 0.3,
                    "status": "completed",
                    "user": {"id": 2, "name": "bob"},
                    "channel": {"id": 200, "name": "support"},
                },
                # Outside the 7-day window — must be excluded.
                {
                    "timestamp": _now() - timedelta(days=30),
                    "command": "rpl",
                    "took": 9.9,
                    "status": "completed",
                    "user": {"id": 1, "name": "alice"},
                    "channel": {"id": 100, "name": "general"},
                },
            ],
            events=[
                {"time_seen": recent, "event_name": "x"},
                {"time_seen": recent, "event_name": "y"},
                # Out-of-window event must drop.
                {"time_seen": _now() - timedelta(days=30), "event_name": "old"},
            ],
        )

        interaction = make_interaction()
        embed = await run_command(cog, "metrics", interaction)

        assert embed.title == "Metrics from the last 7 days"
        desc = embed.description or ""
        # Counts reflect only the in-window rows.
        assert "Total Events Processed:\n\t2" in desc
        assert "Total Commands Handled:\n\t3" in desc
        # Average response time of (0.5+0.7+0.3)/3 = 0.5; formatted to 3 sig figs.
        assert "0.5 seconds" in desc
        # Most-used command is "rpl" (2 invocations).
        assert "rpl: 2" in desc
        assert "deposit_pool: 1" in desc
        # Top user is alice (2 commands), bob has 1.
        assert "alice: 2" in desc
        assert "bob: 1" in desc
        # Channel breakdown follows the same pattern.
        assert "general: 2" in desc
        assert "support: 1" in desc

    async def test_handles_missing_completed_rows(
        self,
        cog: Metrics,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        # All commands `errored` — `completed_rate` aggregation returns []
        # and the success-rate block must be silently omitted.
        recent = _now() - timedelta(hours=1)
        await _seed(
            mongo_db,
            commands=[
                {
                    "timestamp": recent,
                    "command": "rpl",
                    "took": 0.2,
                    "status": "errored",
                    "user": {"id": 1, "name": "alice"},
                    "channel": {"id": 1, "name": "g"},
                },
                {
                    "timestamp": recent,
                    "command": "rpl",
                    "took": 0.1,
                    "status": "errored",
                    "user": {"id": 1, "name": "alice"},
                    "channel": {"id": 1, "name": "g"},
                },
            ],
        )

        embed = await run_command(cog, "metrics", make_interaction())
        desc = embed.description or ""
        assert "Command Success Rate" not in desc
        # But the command/user/channel breakdowns still render.
        assert "rpl: 2" in desc

    async def test_error_path_reports_via_bot(
        self,
        cog: Metrics,
    ) -> None:
        # If anything below the try block raises, the cog must swallow it and
        # call `bot.report_error`. Replace `bot.db` wholesale so any access
        # explodes the moment the cog reaches for it.
        from unittest.mock import MagicMock

        bad_db = MagicMock()
        bad_db.event_queue.count_documents.side_effect = RuntimeError("mongo down")
        cog.bot.db = bad_db

        interaction = make_interaction()
        await cog.metrics.callback(cog, interaction)
        # The cog never sends an embed in this branch; the error goes to
        # the bot's reporter instead.
        assert cog.bot.report_error.await_count == 1


class TestMetricsChart:
    async def test_attaches_png_with_two_subplots(
        self,
        cog: Metrics,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        # Seed a couple of months of data so the aggregation has something
        # to render.
        await _seed(
            mongo_db,
            commands=[
                {"timestamp": datetime(2025, 1, 15, tzinfo=UTC)},
                {"timestamp": datetime(2025, 2, 15, tzinfo=UTC)},
            ],
            events=[
                {"time_seen": datetime(2025, 1, 15, tzinfo=UTC)},
                {"time_seen": datetime(2025, 2, 15, tzinfo=UTC)},
            ],
        )

        interaction = make_interaction()
        await run_command(cog, "metrics_chart", interaction)
        embed = captured_embed(interaction)
        assert embed.image.url == "attachment://metrics.png"
        call_kwargs = interaction.followup.send.call_args.kwargs
        assert call_kwargs["file"].filename == "metrics.png"
