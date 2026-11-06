import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from pymongo.asynchronous.collection import AsyncCollection


@dataclass(frozen=True)
class Spending:
    spent: float
    questions: int


class DailyBudget:
    """What questions may spend per UTC day, kept in MongoDB so restarts don't
    reset it. A running question holds what it may still spend, so questions
    running at the same time can't overspend together."""

    def __init__(
        self,
        collection: AsyncCollection[dict[str, Any]],
        limit: float,
        per_run: float,
        min_run: float,
    ) -> None:
        self.collection = collection
        self.limit = limit
        self.per_run = per_run
        self.min_run = min_run
        self._held: dict[object, float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _today() -> date:
        return datetime.now(UTC).date()

    @classmethod
    def resets_at(cls) -> datetime:
        return datetime.combine(cls._today() + timedelta(days=1), time(), UTC)

    @property
    def held(self) -> float:
        return sum(self._held.values())

    async def _spending_since(self, start: date) -> Spending:
        # days are stored as YYYY-MM-DD, which sort like the dates they are
        cursor = await self.collection.aggregate(
            [
                {"$match": {"_id": {"$gte": start.isoformat()}}},
                {
                    "$group": {
                        "_id": None,
                        "spent": {"$sum": "$spent"},
                        "questions": {"$sum": "$questions"},
                    }
                },
            ]
        )
        docs = await cursor.to_list()
        return (
            Spending(docs[0]["spent"], docs[0]["questions"])
            if docs
            else Spending(0.0, 0)
        )

    async def today(self) -> Spending:
        return await self._spending_since(self._today())

    async def this_week(self) -> Spending:
        today = self._today()
        return await self._spending_since(today - timedelta(days=today.weekday()))

    async def this_month(self) -> Spending:
        return await self._spending_since(self._today().replace(day=1))

    async def spent(self) -> float:
        doc = await self.collection.find_one({"_id": self._today().isoformat()})
        return float(doc["spent"]) if doc else 0.0

    async def _left(self) -> float:
        return self.limit - await self.spent() - self.held

    async def can_start(self) -> bool:
        """Whether a question could get a hold right now."""
        return await self._left() >= self.min_run

    async def hold(self, key: object) -> float | None:
        """Hold up to per_run for a question and return the amount, or None if
        less than min_run is left."""
        async with self._lock:
            amount = min(self.per_run, await self._left())
            if amount < self.min_run:
                return None
            self._held[key] = amount
            return amount

    async def settle(self, key: object, cost: float) -> None:
        """Release a question's hold and record what it actually cost."""
        self._held.pop(key, None)
        if cost > 0:
            await self.collection.update_one(
                {"_id": self._today().isoformat()},
                {"$inc": {"spent": cost, "questions": 1}},
                upsert=True,
            )
