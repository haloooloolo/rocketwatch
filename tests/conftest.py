import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.utils import rocketpool, shared_w3
from rocketwatch.utils.config import (
    Config,
    ConsensusLayerConfig,
    DiscordConfig,
    DiscordOwner,
    DmWarningConfig,
    EventsConfig,
    ExecutionLayerConfig,
    ExecutionLayerEndpoint,
    MongoDBConfig,
    RocketPoolConfig,
    RocketPoolSupport,
    cfg,
)
from tests.lib.scripted_rocketpool import ScriptedRocketPool

# Default the w3/bacon proxies to MagicMocks so existing tests that touch
# `w3.eth.<x>` without setting up a config keep working.
shared_w3.w3._instance = MagicMock()
shared_w3.w3_mainnet._instance = MagicMock()
shared_w3.bacon._instance = MagicMock()

# Install a baseline Config so cfg-reading-at-class-body code (like
# support_utils.SupportUtils.subgroup) imports cleanly. Tests that need
# different values overwrite via `monkeypatch.setattr(cfg, "_instance", ...)`.
cfg._instance = Config(
    discord=DiscordConfig(
        secret="test",
        owner=DiscordOwner(user_id=1, server_id=2),
        channels={"default": 1, "errors": 1},
    ),
    execution_layer=ExecutionLayerConfig(
        explorer="https://etherscan.io",
        endpoint=ExecutionLayerEndpoint(current=["http://localhost"]),
    ),
    consensus_layer=ConsensusLayerConfig(
        explorer="https://beaconcha.in",
        endpoint=["http://localhost"],
    ),
    mongodb=MongoDBConfig(uri="mongodb://localhost"),
    rocketpool=RocketPoolConfig(
        support=RocketPoolSupport(server_id=1, channel_id=1, moderator_id=1),
        dm_warning=DmWarningConfig(channels=[]),
    ),
    events=EventsConfig(lookback_distance=10, genesis=0, block_batch_size=10),
)


@pytest.fixture(scope="session")
def mongo_url() -> Iterator[str]:
    # Lazy import: plain `pytest` deselects integration markers and never hits
    # this fixture, so we don't want to require the testcontainers dep there.
    from testcontainers.mongodb import MongoDbContainer

    with MongoDbContainer("mongo:8") as container:
        yield container.get_connection_url()


@pytest.fixture
async def mongo_db(mongo_url: str) -> AsyncIterator[AsyncDatabase[dict[str, Any]]]:
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(mongo_url)
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
