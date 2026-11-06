from datetime import UTC, date, datetime
from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.ask.budget import DailyBudget, Spending

pytestmark = pytest.mark.integration_db


@pytest.fixture
def budget(mongo_db: AsyncDatabase[dict[str, Any]]) -> DailyBudget:
    return DailyBudget(mongo_db.ask_budget, limit=2.5, per_run=1.0, min_run=0.25)


async def test_holds_up_to_the_per_run_cap(budget: DailyBudget) -> None:
    assert await budget.hold("a") == 1.0


async def test_spending_adds_up_and_survives_a_new_instance(
    budget: DailyBudget,
) -> None:
    await budget.hold("a")
    await budget.settle("a", 0.4)
    await budget.hold("b")
    await budget.settle("b", 0.6)
    again = DailyBudget(budget.collection, limit=2.5, per_run=1.0, min_run=0.25)
    assert await again.spent() == pytest.approx(1.0)


async def test_running_questions_cant_overspend_together(
    budget: DailyBudget,
) -> None:
    assert await budget.hold("a") == 1.0
    assert await budget.hold("b") == 1.0
    assert await budget.hold("c") == pytest.approx(0.5)
    assert await budget.hold("d") is None


async def test_can_start_without_holding(budget: DailyBudget) -> None:
    assert await budget.can_start()
    await budget.hold("a")
    await budget.settle("a", 2.3)
    assert not await budget.can_start()


async def test_counts_spending_and_questions(budget: DailyBudget) -> None:
    await budget.hold("a")
    await budget.settle("a", 0.4)
    await budget.hold("b")
    await budget.settle("b", 0.6)
    today = await budget.today()
    assert today.spent == pytest.approx(1.0)
    assert today.questions == 2


@pytest.fixture
def thursday(monkeypatch: pytest.MonkeyPatch) -> date:
    day = date(2026, 11, 12)
    monkeypatch.setattr(DailyBudget, "_today", staticmethod(lambda: day))
    return day


async def test_totals_cover_their_own_period(
    budget: DailyBudget, mongo_db: AsyncDatabase[dict[str, Any]], thursday: date
) -> None:
    await mongo_db.ask_budget.insert_many(
        [
            {"_id": "2025-11-12", "spent": 100.0, "questions": 100},
            {"_id": "2026-10-31", "spent": 50.0, "questions": 50},
            {"_id": "2026-11-01", "spent": 4.0, "questions": 4},
            {"_id": "2026-11-08", "spent": 2.0, "questions": 2},  # Sunday
            {"_id": "2026-11-09", "spent": 1.0, "questions": 1},  # Monday
        ]
    )
    await budget.hold("a")
    await budget.settle("a", 0.5)

    assert await budget.today() == Spending(0.5, 1)
    assert await budget.this_week() == Spending(1.5, 2)
    assert await budget.this_month() == Spending(7.5, 8)


async def test_days_recorded_without_a_question_count_still_add_up(
    budget: DailyBudget, mongo_db: AsyncDatabase[dict[str, Any]], thursday: date
) -> None:
    await mongo_db.ask_budget.insert_one({"_id": "2026-11-02", "spent": 1.0})
    assert await budget.this_month() == Spending(1.0, 0)


async def test_nothing_spent_yet(budget: DailyBudget) -> None:
    assert await budget.today() == Spending(0.0, 0)


async def test_held_is_what_running_questions_reserved(budget: DailyBudget) -> None:
    await budget.hold("a")
    await budget.hold("b")
    assert budget.held == 2.0
    await budget.settle("a", 0.1)
    assert budget.held == 1.0


async def test_resets_at_the_next_utc_midnight() -> None:
    reset = DailyBudget.resets_at()
    now = datetime.now(UTC)
    assert reset > now
    assert (reset.hour, reset.minute, reset.second) == (0, 0, 0)
    assert (reset - now).total_seconds() <= 24 * 3600


async def test_refuses_below_the_minimum(budget: DailyBudget) -> None:
    await budget.hold("a")
    await budget.settle("a", 2.3)
    assert await budget.hold("b") is None


async def test_settling_releases_the_hold(budget: DailyBudget) -> None:
    for key in "abc":
        await budget.hold(key)
    await budget.settle("a", 0.1)
    assert await budget.hold("d") == pytest.approx(0.9)
