from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from hexbytes import HexBytes

from rocketwatch.plugins.log_events import aggregation as agg_module
from rocketwatch.plugins.log_events.aggregation import (
    aggregate_events,
    get_event_name,
)
from tests.lib.scripted_rocketpool import ScriptedRocketPool, addr

# Topic sentinels → mapped to event names via topic_map.
T_BURN = HexBytes(b"\x01" * 32)
T_TRANSFER = HexBytes(b"\x02" * 32)
T_ASSIGNED = HexBytes(b"\x03" * 32)
T_MEGA_ASSIGNED = HexBytes(b"\x04" * 32)
T_PRESTAKE = HexBytes(b"\x05" * 32)
T_WITHDRAWAL = HexBytes(b"\x06" * 32)

TOPIC_MAP = {
    T_BURN.hex(): "TokensBurned",
    T_TRANSFER.hex(): "Transfer",
    T_ASSIGNED.hex(): "DepositAssigned",
    T_MEGA_ASSIGNED.hex(): "MegapoolValidatorAssigned",
    T_PRESTAKE.hex(): "MinipoolPrestaked",
    T_WITHDRAWAL.hex(): "WithdrawalRequested",
}

RETH = addr("0x" + "11" * 20)
DEPOSIT_POOL = addr("0x" + "22" * 20)
MEGA_A = addr("0x" + "33" * 20)
MEGA_B = addr("0x" + "44" * 20)
UNSTETH = addr("0x" + "55" * 20)
MINIPOOL = addr("0x" + "66" * 20)


def _log(
    *,
    tx: str,
    address: str,
    topic: HexBytes,
    args: dict[str, Any] | None = None,
    extra_topics: list[HexBytes] | None = None,
) -> dict[str, Any]:
    topics = [topic, *(extra_topics or [])]
    log: dict[str, Any] = {
        "transactionHash": tx,
        "address": address,
        "topics": topics,
    }
    if args is not None:
        log["_args"] = args
    return log


class _ScriptedProc:
    @staticmethod
    def process_log(event: dict[str, Any]) -> dict[str, Any]:
        return {"args": event["_args"]}


class _ScriptedEvents:
    def __getitem__(self, _name: str) -> Any:
        return lambda: _ScriptedProc()


class _ScriptedContract:
    events = _ScriptedEvents()


@pytest.fixture
def _scripted(scripted_rp: ScriptedRocketPool) -> Iterator[ScriptedRocketPool]:
    scripted_rp.set_address("rocketTokenRETH", RETH)
    scripted_rp.set_address("rocketDepositPool", DEPOSIT_POOL)
    scripted_rp.set_address("unstETH", UNSTETH)
    # Decode path: contract.events[name]().process_log(event).
    scripted_rp.get_contract_by_address = AsyncMock(  # type: ignore[method-assign]
        return_value=_ScriptedContract()
    )
    yield scripted_rp


class TestGetEventName:
    def test_log_receipt_uses_topic_map_and_contract(
        self, _scripted: ScriptedRocketPool
    ) -> None:
        event = _log(tx="0xtx", address=RETH, topic=T_BURN)
        name, full = get_event_name(event, TOPIC_MAP)
        assert name == "TokensBurned"
        assert full == "rocketTokenRETH.TokensBurned"

    def test_event_data_without_topics(self) -> None:
        # EventData (no "topics") falls back to the "event" key, no contract.
        event = {"event": "SomethingHappened", "transactionHash": "0xtx"}
        name, full = get_event_name(event, TOPIC_MAP)
        assert name == "SomethingHappened"
        assert full == "SomethingHappened"

    def test_unknown_address_yields_bare_name(
        self, _scripted: ScriptedRocketPool
    ) -> None:
        event = _log(tx="0xtx", address=addr("0x" + "99" * 20), topic=T_TRANSFER)
        name, full = get_event_name(event, TOPIC_MAP)
        assert name == "Transfer"
        # No contract name resolved → bare event name.
        assert full == "Transfer"


class TestConflictRules:
    async def test_tokens_burned_removes_transfer(
        self, _scripted: ScriptedRocketPool
    ) -> None:
        burn = _log(tx="0xtx", address=RETH, topic=T_BURN)
        transfer = _log(tx="0xtx", address=RETH, topic=T_TRANSFER, args={"value": 5})
        result = await aggregate_events([burn, transfer], TOPIC_MAP)
        # Transfer is dropped; only the burn survives.
        assert len(result) == 1
        assert result[0]["topics"][0] == T_BURN

    async def test_no_conflict_across_different_transactions(
        self, _scripted: ScriptedRocketPool
    ) -> None:
        burn = _log(tx="0xtx1", address=RETH, topic=T_BURN)
        transfer = _log(tx="0xtx2", address=RETH, topic=T_TRANSFER, args={"value": 5})
        result = await aggregate_events([burn, transfer], TOPIC_MAP)
        # Different tx → conflict rule doesn't fire, both survive.
        assert len(result) == 2


class TestAggregationRules:
    async def test_counts_deposit_assignments(
        self, _scripted: ScriptedRocketPool
    ) -> None:
        events = [
            _log(tx="0xtx", address=DEPOSIT_POOL, topic=T_ASSIGNED) for _ in range(3)
        ]
        result = await aggregate_events(events, TOPIC_MAP)
        assert len(result) == 1
        assert result[0]["assignmentCount"] == 3

    async def test_grouped_count_by_address(
        self, _scripted: ScriptedRocketPool
    ) -> None:
        events = [
            _log(tx="0xtx", address=MEGA_A, topic=T_MEGA_ASSIGNED),
            _log(tx="0xtx", address=MEGA_A, topic=T_MEGA_ASSIGNED),
            _log(tx="0xtx", address=MEGA_B, topic=T_MEGA_ASSIGNED),
        ]
        result = await aggregate_events(events, TOPIC_MAP)
        # One surviving event per address group, with per-group counts.
        counts = sorted(e["assignmentCount"] for e in result)
        assert counts == [1, 2]


class TestDeduplicationRule:
    async def test_keeps_highest_transfer_value(
        self, _scripted: ScriptedRocketPool
    ) -> None:
        low = _log(tx="0xtx", address=RETH, topic=T_TRANSFER, args={"value": 10})
        high = _log(tx="0xtx", address=RETH, topic=T_TRANSFER, args={"value": 99})
        result = await aggregate_events([low, high], TOPIC_MAP)
        assert len(result) == 1
        assert result[0]["_args"]["value"] == 99


class TestSumAggregation:
    async def test_sums_withdrawal_amounts(self, _scripted: ScriptedRocketPool) -> None:
        events = [
            _log(
                tx="0xtx",
                address=UNSTETH,
                topic=T_WITHDRAWAL,
                args={"amountOfStETH": 100},
            ),
            _log(
                tx="0xtx",
                address=UNSTETH,
                topic=T_WITHDRAWAL,
                args={"amountOfStETH": 250},
            ),
        ]
        result = await aggregate_events(events, TOPIC_MAP)
        assert len(result) == 1
        assert result[0]["amountOfStETH"] == 350


class TestMatchBasedConflict:
    async def test_prestake_removes_matching_deposit_assigned(
        self, _scripted: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The MinipoolPrestaked↔DepositAssigned rule matches on the minipool
        # address encoded in the loser's second topic.
        monkeypatch.setattr(
            agg_module.w3, "to_checksum_address", lambda _b: MINIPOOL, raising=False
        )
        # Winner: prestake event at the minipool address (unknown contract →
        # bare name "MinipoolPrestaked").
        prestake = _log(tx="0xtx", address=MINIPOOL, topic=T_PRESTAKE)
        # Loser: a deposit assignment whose topic[1] decodes to the minipool.
        assigned = _log(
            tx="0xtx",
            address=DEPOSIT_POOL,
            topic=T_ASSIGNED,
            extra_topics=[HexBytes(b"\x00" * 12 + b"\x66" * 20)],
        )
        result = await aggregate_events([prestake, assigned], TOPIC_MAP)
        # The matching DepositAssigned is removed; only the prestake remains.
        assert len(result) == 1
        assert result[0]["topics"][0] == T_PRESTAKE
