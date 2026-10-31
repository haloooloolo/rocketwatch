from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.ask.budget import DailyBudget

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


async def test_refuses_below_the_minimum(budget: DailyBudget) -> None:
    await budget.hold("a")
    await budget.settle("a", 2.3)
    assert await budget.hold("b") is None


async def test_settling_releases_the_hold(budget: DailyBudget) -> None:
    for key in "abc":
        await budget.hold(key)
    await budget.settle("a", 0.1)
    assert await budget.hold("d") == pytest.approx(0.9)
