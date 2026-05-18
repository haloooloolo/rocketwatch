from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from rocketwatch.plugins.queue.queue import Queue
from rocketwatch.utils import shared_w3
from tests.lib.discord_harness import make_bot, make_interaction
from tests.lib.scripted_rocketpool import ScriptedRocketPool

# Distinct namespace bytes so the scripted ScriptedRocketPool can route
# scan/getLength calls by namespace. Using stable sentinels avoids needing a
# real keccak. The Queue code calls w3.solidity_keccak — we stub it below.
EXPRESS_NS = b"\xee" * 32
STANDARD_NS = b"\x55" * 32


@pytest.fixture
def _stub_queue_w3(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The baseline w3 is a MagicMock; we need:
    # - solidity_keccak to return our distinct namespace bytes
    # - eth.get_block_number to be a callable returning an int (not a MagicMock)
    def keccak(_types: list[str], data: list[str]) -> bytes:
        return EXPRESS_NS if data[0].endswith(".express") else STANDARD_NS

    monkeypatch.setattr(shared_w3.w3, "solidity_keccak", keccak, raising=False)

    async def block_number() -> int:
        return 12_345

    eth = MagicMock()
    eth.get_block_number = block_number
    monkeypatch.setattr(shared_w3.w3, "eth", eth, raising=False)
    yield


@pytest.fixture(autouse=True)
def _stub_format_helpers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # _format_queue_entry → _megapool_to_node → rp.call. Replace with a
    # deterministic stand-in so tests can focus on slot ordering / counts.
    async def fake_format(entry: Queue.Entry) -> str:
        return f"<mp={entry.megapool},vid={entry.validator_id}>"

    monkeypatch.setattr(
        "rocketwatch.plugins.queue.queue.Queue._Queue__format_queue_entry",
        fake_format,
        raising=False,
    )
    # The aiocache-decorated _cached_el_url persists across tests; bypass.
    monkeypatch.setattr(
        "rocketwatch.plugins.queue.queue.Queue._cached_el_url",
        AsyncMock(side_effect=lambda addr, prefix="": f"[{addr}](el/{addr})"),
        raising=False,
    )
    yield


def _entry(vid: int, mp: str = "0xMP") -> Queue.Entry:
    return Queue.Entry(megapool=mp, validator_id=vid, bond=4_000, deposit_size=32_000)


def _seed_queue(
    scripted_rp: ScriptedRocketPool,
    *,
    express: list[Queue.Entry],
    standard: list[Queue.Entry],
    queue_index: int = 0,
    express_rate: int = 2,
) -> None:
    """Wire scripted contract calls for a fixed queue state."""

    def get_length(ns: bytes) -> int:
        return len(express) if ns == EXPRESS_NS else len(standard)

    def scan(ns: bytes, _start_idx: int, end: int) -> tuple[list[tuple[Any, ...]], int]:
        # The cog's _scan_list pulls `[start:]` itself, so just return the
        # raw prefix of length min(end, len) along with a placeholder tail.
        entries = express if ns == EXPRESS_NS else standard
        raw = [tuple(e) for e in entries[:end]]
        return raw, 0

    scripted_rp.set_call("linkedListStorage.getLength", get_length)
    scripted_rp.set_call("linkedListStorage.scan", scan)
    scripted_rp.set_call(
        "rocketDAOProtocolSettingsDeposit.getExpressQueueRate", express_rate
    )
    scripted_rp.set_call("rocketDepositPool.getQueueIndex", queue_index)


class TestEntriesUsedInInterval:
    @pytest.mark.parametrize(
        ("start", "end", "rate", "len_e", "len_s", "expected"),
        [
            # No entries used at all — interval is empty (start > end).
            # (skipped: cog only calls this with end >= start)
            # Pure express slots when standard queue is empty.
            (0, 2, 2, 100, 0, (3, 0)),
            # First three slots with rate=2 → e, e, s.
            (0, 2, 2, 100, 100, (2, 1)),
            # Wrapping across a standard slot in the middle.
            (1, 5, 2, 100, 100, (3, 2)),
            # Express queue exhausted → fill from standard.
            (0, 5, 2, 1, 100, (1, 5)),
            # Standard queue exhausted → fill from express.
            (0, 5, 2, 100, 1, (5, 1)),
        ],
    )
    def test_distribution(
        self,
        start: int,
        end: int,
        rate: int,
        len_e: int,
        len_s: int,
        expected: tuple[int, int],
    ) -> None:
        assert (
            Queue._get_entries_used_in_interval(start, end, len_e, len_s, rate)
            == expected
        )


class TestGetStandardQueue:
    async def test_empty_queue_returns_empty_string(
        self,
        scripted_rp: ScriptedRocketPool,
        _stub_queue_w3: None,
    ) -> None:
        _seed_queue(scripted_rp, express=[], standard=[])
        length, content = await Queue.get_standard_queue(limit=10)
        assert length == 0
        assert content == ""

    async def test_lists_entries_in_order(
        self,
        scripted_rp: ScriptedRocketPool,
        _stub_queue_w3: None,
    ) -> None:
        _seed_queue(
            scripted_rp,
            express=[],
            standard=[_entry(1), _entry(2), _entry(3)],
        )
        length, content = await Queue.get_standard_queue(limit=10)
        assert length == 3
        # Entries are 1-indexed in the rendered output.
        assert content.startswith("1. <mp=0xMP,vid=1>")
        assert "2. <mp=0xMP,vid=2>" in content
        assert "3. <mp=0xMP,vid=3>" in content

    async def test_zero_limit_short_circuits(
        self,
        scripted_rp: ScriptedRocketPool,
        _stub_queue_w3: None,
    ) -> None:
        # No contract calls needed for limit==0; ensure no scripting issues.
        length, content = await Queue.get_standard_queue(limit=0)
        assert length == 0
        assert content == ""

    async def test_start_past_end_returns_length_no_content(
        self,
        scripted_rp: ScriptedRocketPool,
        _stub_queue_w3: None,
    ) -> None:
        _seed_queue(scripted_rp, express=[], standard=[_entry(1)])
        length, content = await Queue.get_standard_queue(limit=10, start=5)
        assert length == 1
        assert content == ""


class TestGetExpressQueue:
    async def test_routes_to_express_namespace(
        self,
        scripted_rp: ScriptedRocketPool,
        _stub_queue_w3: None,
    ) -> None:
        _seed_queue(
            scripted_rp,
            express=[_entry(11), _entry(12)],
            standard=[_entry(99)],
        )
        length, content = await Queue.get_express_queue(limit=10)
        assert length == 2
        assert "vid=11" in content
        assert "vid=12" in content
        assert "vid=99" not in content


class TestGetCombinedQueue:
    async def test_interleaves_express_and_standard(
        self,
        scripted_rp: ScriptedRocketPool,
        _stub_queue_w3: None,
    ) -> None:
        _seed_queue(
            scripted_rp,
            express=[_entry(1), _entry(2)],
            standard=[_entry(100)],
            queue_index=0,
            express_rate=2,
        )
        length, content = await Queue.get_combined_queue(limit=10)
        assert length == 3
        # rate=2 → slots 0,1 are express; slot 2 is standard.
        lines = content.strip().split("\n")
        assert "🐇" in lines[0]
        assert "🐇" in lines[1]
        assert "🐢" in lines[2]

    async def test_start_past_end_returns_length_no_content(
        self,
        scripted_rp: ScriptedRocketPool,
        _stub_queue_w3: None,
    ) -> None:
        _seed_queue(
            scripted_rp,
            express=[_entry(1)],
            standard=[_entry(2)],
        )
        length, content = await Queue.get_combined_queue(limit=10, start=20)
        assert length == 2
        assert content == ""


class TestValidatorPageView:
    def test_combined_lane_picks_combined_loader(self) -> None:
        view = Queue.ValidatorPageView("combined")
        assert view._title == "Validator Queue"
        assert view.content_loader is Queue.get_combined_queue

    def test_standard_lane_picks_standard_loader(self) -> None:
        view = Queue.ValidatorPageView("standard")
        assert "Standard" in view._title
        assert view.content_loader is Queue.get_standard_queue

    def test_express_lane_picks_express_loader(self) -> None:
        view = Queue.ValidatorPageView("express")
        assert "Express" in view._title
        assert view.content_loader is Queue.get_express_queue


class TestQueueCommand:
    async def test_sends_embed_with_pageview(
        self,
        scripted_rp: ScriptedRocketPool,
        _stub_queue_w3: None,
    ) -> None:
        _seed_queue(
            scripted_rp,
            express=[_entry(1)],
            standard=[_entry(2)],
        )
        cog = Queue(make_bot())
        interaction = make_interaction()
        await cog.queue.callback(cog, interaction, lane="standard")

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs["view"].queue_name.endswith("Standard Queue")
