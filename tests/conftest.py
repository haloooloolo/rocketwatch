import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.utils import rocketpool, shared_w3
from rocketwatch.utils.config import cfg
from tests.lib.beacon_script import ScriptedBeacon
from tests.lib.cfg import make_cfg
from tests.lib.event_log_script import EventLogScript
from tests.lib.scripted_rocketpool import ScriptedRocketPool

# Default the w3/bacon proxies to MagicMocks so existing tests that touch
# `w3.eth.<x>` without setting up a config keep working.
shared_w3.w3._instance = MagicMock()
shared_w3.w3_mainnet._instance = MagicMock()
shared_w3.bacon._instance = MagicMock()

# Install a baseline Config so cfg-reading-at-class-body code (like
# support_utils.SupportUtils.subgroup) imports cleanly. Tests that need
# different values overwrite via `monkeypatch.setattr(cfg, "_instance", ...)`.
cfg._instance = make_cfg()


@pytest.fixture
def mainnet_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "_instance", make_cfg("mainnet"))


@pytest.fixture
def testnet_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "_instance", make_cfg("holesky"))


@pytest.fixture(scope="session")
def mongo_url() -> Iterator[str]:
    # Lazy import: plain `pytest` deselects integration markers and never hits
    # this fixture, so we don't want to require the testcontainers dep there.
    from testcontainers.mongodb import MongoDbContainer

    with MongoDbContainer("mongo:8") as container:
        yield container.get_connection_url()


@pytest.fixture
async def mongo_db(mongo_url: str) -> AsyncIterator[AsyncDatabase[dict[str, Any]]]:
    # tz_aware=True mirrors the production client (bot.py); datetimes round-trip
    # as UTC-aware so naive/aware comparison bugs surface in tests too.
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
        mongo_url, tz_aware=True
    )
    db_name = f"rw_test_{uuid.uuid4().hex[:8]}"
    try:
        yield client[db_name]
    finally:
        await client.drop_database(db_name)
        await client.aclose()


@pytest.fixture
def scripted_rp(monkeypatch: pytest.MonkeyPatch) -> ScriptedRocketPool:
    scripted = ScriptedRocketPool()
    monkeypatch.setattr(rocketpool.rp, "_instance", scripted)
    return scripted


@pytest.fixture
def scripted_bacon(monkeypatch: pytest.MonkeyPatch) -> ScriptedBeacon:
    scripted = ScriptedBeacon()
    monkeypatch.setattr(shared_w3.bacon, "_instance", scripted)
    return scripted


@pytest.fixture
def event_log_script(monkeypatch: pytest.MonkeyPatch) -> EventLogScript:
    # Replace the proxy's `_instance` with a fresh mock that delegates
    # `eth.get_logs` and `eth.get_block_number` to the script.
    script = EventLogScript()

    eth_stub = MagicMock()
    eth_stub.get_logs = script.get_logs
    # Default to a high block number so tests using `toBlock="latest"` work.
    eth_stub.get_block_number = MagicMock(return_value=2**31, side_effect=None)

    async def _async_block_number() -> int:
        return 2**31

    eth_stub.get_block_number = _async_block_number

    w3_stub = MagicMock()
    w3_stub.eth = eth_stub
    monkeypatch.setattr(shared_w3.w3, "_instance", w3_stub)
    return script
