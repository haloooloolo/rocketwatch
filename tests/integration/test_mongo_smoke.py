from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase

pytestmark = pytest.mark.integration_db


async def test_insert_and_find_round_trip(
    mongo_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await mongo_db.events.insert_one({"_id": "abc", "block": 1234})
    doc = await mongo_db.events.find_one({"_id": "abc"})
    assert doc is not None
    assert doc["block"] == 1234


async def test_databases_are_isolated_per_test(
    mongo_db: AsyncDatabase[dict[str, Any]],
) -> None:
    # Sanity-check the per-test db_name isolation: prior test's _id shouldn't leak.
    assert await mongo_db.events.find_one({"_id": "abc"}) is None
    await mongo_db.events.insert_one({"_id": "xyz", "block": 5678})
    assert (await mongo_db.events.count_documents({})) == 1


async def test_aggregation_pipelines_run(
    mongo_db: AsyncDatabase[dict[str, Any]],
) -> None:
    # Aggregation parity is the load-bearing reason we use real Mongo over mongomock.
    await mongo_db.events.insert_many(
        [
            {"kind": "deposit", "amount": 1},
            {"kind": "deposit", "amount": 2},
            {"kind": "withdraw", "amount": 5},
        ]
    )
    pipeline: list[dict[str, Any]] = [
        {"$group": {"_id": "$kind", "total": {"$sum": "$amount"}}},
        {"$sort": {"_id": 1}},
    ]
    results = [doc async for doc in await mongo_db.events.aggregate(pipeline)]
    assert results == [
        {"_id": "deposit", "total": 3},
        {"_id": "withdraw", "total": 5},
    ]
