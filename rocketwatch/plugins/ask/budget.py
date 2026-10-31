import asyncio
from datetime import UTC, datetime, time, timedelta
from typing import Any

from pymongo.asynchronous.collection import AsyncCollection


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
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()

    @staticmethod
    def resets_at() -> datetime:
        tomorrow = datetime.now(UTC).date() + timedelta(days=1)
        return datetime.combine(tomorrow, time(), UTC)

    async def spent(self) -> float:
        doc = await self.collection.find_one({"_id": self._today()})
        return float(doc["spent"]) if doc else 0.0

    async def hold(self, key: object) -> float | None:
        """Hold up to per_run for a question and return the amount, or None if
        less than min_run is left."""
        async with self._lock:
            left = self.limit - await self.spent() - sum(self._held.values())
            amount = min(self.per_run, left)
            if amount < self.min_run:
                return None
            self._held[key] = amount
            return amount

    async def settle(self, key: object, cost: float) -> None:
        """Release a question's hold and record what it actually cost."""
        self._held.pop(key, None)
        if cost > 0:
            await self.collection.update_one(
                {"_id": self._today()}, {"$inc": {"spent": cost}}, upsert=True
            )
