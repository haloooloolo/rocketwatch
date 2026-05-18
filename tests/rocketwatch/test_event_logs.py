from typing import Any
from unittest.mock import AsyncMock

import pytest
from eth_typing import BlockNumber
from hexbytes import HexBytes

from rocketwatch.utils.event_logs import get_logs
from tests.lib.event_log_script import EventLogScript, make_log


class TestArgumentValidation:
    async def test_address_agnostic_with_arg_filters_raises(self) -> None:
        # arg_filters is only meaningful when the event object is queried
        # directly; the address-agnostic branch uses w3.eth.get_logs with
        # only a topic filter, so the combination is rejected up front.
        event = AsyncMock()
        with pytest.raises(ValueError, match="arg_filters"):
            await get_logs(
                event,
                BlockNumber(0),
                BlockNumber(10),
                arg_filters={"node": "0xabc"},
                address_agnostic=True,
            )


class TestAddressAgnosticPath:
    async def test_filters_by_topic_and_aggregates_chunks(
        self, event_log_script: EventLogScript
    ) -> None:
        # Two logs at different block heights, same topic. Both should come
        # through across the chunked range.
        topic = HexBytes(b"\x11" * 32)
        event_log_script.add_many(
            [
                make_log(address="0xaaa", topics=[topic], block_number=10),
                make_log(address="0xbbb", topics=[topic], block_number=90_000),
                # Different topic — must be filtered out.
                make_log(
                    address="0xaaa", topics=[HexBytes(b"\x22" * 32)], block_number=50
                ),
            ]
        )

        event = AsyncMock()
        event.topic = topic
        # The address-agnostic path uses `event.process_log` to decode raw logs.
        event.process_log = lambda entry: {"decoded": entry["address"]}

        result = await get_logs(
            event,
            BlockNumber(0),
            BlockNumber(100_000),
            address_agnostic=True,
        )
        assert sorted(r["decoded"] for r in result) == ["0xaaa", "0xbbb"]

    async def test_single_chunk_when_range_fits(
        self, event_log_script: EventLogScript
    ) -> None:
        topic = HexBytes(b"\x33" * 32)
        event_log_script.add(make_log(address="0xaaa", topics=[topic], block_number=5))
        event = AsyncMock()
        event.topic = topic
        event.process_log = lambda entry: entry["address"]

        # Range of 10 blocks fits inside the 50_000 chunk size → one HTTP call.
        result = await get_logs(
            event, BlockNumber(0), BlockNumber(10), address_agnostic=True
        )
        assert result == ["0xaaa"]


class TestEventGetLogsPath:
    async def test_chunks_request_at_50k_boundary(self) -> None:
        # Track which (from, to) ranges the event's get_logs was called with.
        seen_ranges: list[tuple[int, int]] = []

        async def fake_get_logs(
            *, from_block: BlockNumber, to_block: BlockNumber, argument_filters: Any
        ) -> list[dict[str, Any]]:
            seen_ranges.append((int(from_block), int(to_block)))
            return [{"chunk": (int(from_block), int(to_block))}]

        event = AsyncMock()
        event.get_logs = fake_get_logs

        result = await get_logs(event, BlockNumber(0), BlockNumber(120_000))
        # chunk_end = chunk_start + 50_000 (inclusive on both ends), so each
        # window is 50_001 blocks wide.
        assert seen_ranges == [
            (0, 50_000),
            (50_001, 100_001),
            (100_002, 120_000),
        ]
        assert len(result) == 3

    async def test_arg_filters_passed_through(self) -> None:
        received: dict[str, Any] = {}

        async def fake_get_logs(
            *, from_block: BlockNumber, to_block: BlockNumber, argument_filters: Any
        ) -> list[dict[str, Any]]:
            received["arg_filters"] = argument_filters
            return []

        event = AsyncMock()
        event.get_logs = fake_get_logs

        await get_logs(
            event,
            BlockNumber(0),
            BlockNumber(10),
            arg_filters={"node": "0xabc"},
        )
        assert received["arg_filters"] == {"node": "0xabc"}
