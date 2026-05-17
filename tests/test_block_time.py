from unittest.mock import AsyncMock

import pytest

from rocketwatch.utils import block_time
from rocketwatch.utils.block_time import ts_to_block


@pytest.fixture
def fake_chain(monkeypatch):
    """Install a deterministic block→timestamp lookup and latest-block number.

    Usage in tests: ``fake_chain({1: 10, 2: 30, ...}, latest=N)``.
    The ``block_to_ts`` async function is replaced with a lookup over the dict,
    and ``w3.eth.get_block_number`` is stubbed to return ``latest``.
    """

    def configure(ts_map: dict[int, int], latest: int) -> None:
        async def fake_block_to_ts(n: int) -> int:
            return ts_map[n]

        monkeypatch.setattr(block_time, "block_to_ts", fake_block_to_ts)
        monkeypatch.setattr(
            block_time.w3.eth,
            "get_block_number",
            AsyncMock(return_value=latest),
        )

    return configure


class TestTsToBlock:
    async def test_target_before_genesis_returns_zero(self, fake_chain):
        # Per the doc-comment in the function, the genesis block doesn't carry
        # a timestamp; anything earlier than block 1's timestamp maps to 0.
        fake_chain({1: 10, 2: 30}, latest=2)
        assert await ts_to_block(5) == 0

    async def test_exact_match_returns_that_block(self, fake_chain):
        fake_chain({1: 10, 2: 30, 3: 50, 4: 70, 5: 90}, latest=5)
        assert await ts_to_block(50) == 3

    async def test_exact_match_at_lower_bound(self, fake_chain):
        # When the target equals block 1's timestamp exactly, it's a match,
        # not a "before genesis" case.
        fake_chain({1: 10, 2: 30, 3: 50}, latest=3)
        assert await ts_to_block(10) == 1

    async def test_no_exact_match_returns_closer_of_two_neighbors(self, fake_chain):
        # When there's no block at the target ts, the answer must be one of the
        # two adjacent blocks — whichever has the smaller |Δt|.
        fake_chain({1: 10, 2: 30, 3: 35, 4: 100}, latest=4)
        # target=15: |10-15|=5, |30-15|=15 → block 1 is closer.
        assert await ts_to_block(15) == 1
        # target=27: |30-27|=3, |10-27|=17 → block 2 is closer.
        assert await ts_to_block(27) == 2

    async def test_picks_closer_neighbor_in_dense_region(self, fake_chain):
        # Same idea, narrower gaps — the function should still pick the strictly
        # closer of the two adjacent blocks.
        fake_chain({1: 10, 2: 30, 3: 35, 4: 100}, latest=4)
        # target=33: |35-33|=2, |30-33|=3 → block 3 wins.
        assert await ts_to_block(33) == 3

    async def test_target_after_latest_returns_latest(self, fake_chain):
        # A ts beyond the chain head should still resolve to a real block —
        # the closest one available, which is the latest block.
        fake_chain({1: 10, 2: 30, 3: 35, 4: 100}, latest=4)
        assert await ts_to_block(10_000) == 4

    async def test_result_is_always_a_real_block_number(self, fake_chain):
        # Property: the returned block must be one whose timestamp exists in
        # the chain (or 0 for the pre-genesis case).
        ts_map = {1: 10, 2: 30, 3: 35, 4: 100, 5: 200, 6: 250}
        fake_chain(ts_map, latest=6)
        for target in [11, 20, 33, 60, 150, 220, 245, 1_000_000]:
            result = await ts_to_block(target)
            assert result in ts_map, f"target={target} produced non-block {result}"
