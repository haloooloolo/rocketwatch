from typing import cast
from unittest.mock import AsyncMock

import pytest
from eth_typing import ChecksumAddress

from rocketwatch.utils import sea_creatures as sc_module
from rocketwatch.utils.sea_creatures import (
    get_holding_for_address,
    get_sea_creature_for_address,
    get_sea_creature_for_holdings,
    price_cache,
    sea_creatures,
)
from tests.lib.scripted_rocketpool import ScriptedRocketPool


@pytest.fixture(autouse=True)
def _reset_price_cache():
    # The module caches RPL/rETH prices keyed by block; tests share process
    # state, so clear it between cases.
    price_cache["block"] = 0.0
    price_cache["rpl_price"] = 0.0
    price_cache["reth_price"] = 0.0
    yield
    price_cache["block"] = 0.0
    price_cache["rpl_price"] = 0.0
    price_cache["reth_price"] = 0.0


class TestGetSeaCreatureForHoldings:
    def test_below_smallest_threshold(self):
        assert get_sea_creature_for_holdings(0.5) == ""

    def test_zero_holdings(self):
        assert get_sea_creature_for_holdings(0) == ""

    def test_smallest_threshold_microbe(self):
        assert get_sea_creature_for_holdings(1) == sea_creatures[1]

    def test_snail_at_threshold(self):
        assert get_sea_creature_for_holdings(5) == sea_creatures[5]

    def test_crab_just_at_threshold(self):
        assert get_sea_creature_for_holdings(64) == sea_creatures[64]

    def test_crab_below_octopus(self):
        # 159 should still be crab (64 <= 159 < 160)
        assert get_sea_creature_for_holdings(159) == sea_creatures[64]

    def test_octopus_just_at_threshold(self):
        assert get_sea_creature_for_holdings(160) == sea_creatures[160]

    def test_whale_at_highest_threshold(self):
        assert get_sea_creature_for_holdings(3200) == sea_creatures[3200]

    def test_just_below_double_max_returns_single_whale(self):
        # 2 * 3200 = 6400; 6399 is still single whale.
        assert get_sea_creature_for_holdings(6399) == sea_creatures[3200]

    def test_double_max_returns_two_whales(self):
        assert get_sea_creature_for_holdings(6400) == sea_creatures[3200] * 2

    def test_intermediate_multiplier(self):
        # 5 * 3200 = 16,000 → 5 whales.
        assert get_sea_creature_for_holdings(16000) == sea_creatures[3200] * 5

    def test_multiplier_capped_at_ten(self):
        # Even absurd amounts cap to 10 whales.
        assert get_sea_creature_for_holdings(10**9) == sea_creatures[3200] * 10

    def test_multiplier_cap_boundary_inclusive(self):
        # Exactly 10x: should give 10 whales, not more.
        assert get_sea_creature_for_holdings(32000) == sea_creatures[3200] * 10

    def test_fractional_above_threshold(self):
        # 5.4 → 🐌 (5 threshold)
        assert get_sea_creature_for_holdings(5.4) == sea_creatures[5]


ETH = 10**18
ADDR = cast(ChecksumAddress, "0x" + "11" * 20)


def _patch_w3_eth(
    monkeypatch: pytest.MonkeyPatch, *, block_number: int, eth_balance_wei: int
) -> None:
    """Install AsyncMock stubs for the two w3.eth methods the cog uses."""
    monkeypatch.setattr(sc_module.w3, "eth", type("E", (), {})(), raising=False)
    sc_module.w3.eth.get_block_number = AsyncMock(return_value=block_number)
    sc_module.w3.eth.get_balance = AsyncMock(return_value=eth_balance_wei)


def _patch_token_contracts_returning_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make the RPL/rPLFS/rETH token contract path raise so the
    `contextlib.suppress(Exception)` branch in get_holding_for_address fires."""

    async def _explode(name: str, mainnet: bool = False) -> object:
        raise RuntimeError("no token contracts in this test")

    monkeypatch.setattr(sc_module.rp, "get_contract_by_name", _explode)


class TestGetHoldingForAddress:
    async def test_sums_eth_balance_plus_bonded_plus_staked_rpl(
        self,
        monkeypatch: pytest.MonkeyPatch,
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        # 100 ETH liquid + 32 ETH bonded + 200 RPL @ 0.05 ETH = 142 ETH total.
        _patch_w3_eth(monkeypatch, block_number=1, eth_balance_wei=100 * ETH)
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", 5 * 10**16)
        scripted_rp.set_call("rocketTokenRETH.getExchangeRate", 1 * ETH)
        scripted_rp.set_call("rocketNodeStaking.getNodeETHBonded", 32 * ETH)
        scripted_rp.set_call("rocketNodeStaking.getNodeStakedRPL", 200 * ETH)

        _patch_token_contracts_returning_zero(monkeypatch)

        total = await get_holding_for_address(ADDR)
        # 100 + 32 + (200 * 0.05) = 142.
        assert total == pytest.approx(142.0)

    async def test_price_cache_is_skipped_when_block_unchanged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        # Pre-populate the cache for block 5 with known prices. The second
        # call should NOT re-fetch — script the prices to obviously-wrong
        # values and assert the cached numbers won.
        _patch_w3_eth(monkeypatch, block_number=5, eth_balance_wei=0)
        price_cache["block"] = 5
        price_cache["rpl_price"] = 0.10
        price_cache["reth_price"] = 1.0

        # Script "wrong" prices that would change the result if the cache
        # were ignored.
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", 999 * ETH)
        scripted_rp.set_call("rocketTokenRETH.getExchangeRate", 999 * ETH)
        scripted_rp.set_call("rocketNodeStaking.getNodeETHBonded", 0)
        scripted_rp.set_call("rocketNodeStaking.getNodeStakedRPL", 10 * ETH)

        _patch_token_contracts_returning_zero(monkeypatch)

        total = await get_holding_for_address(ADDR)
        # bonded=0, staked_rpl=10 RPL * cached price 0.10 = 1.0.
        assert total == pytest.approx(1.0)

    async def test_price_cache_refreshes_on_new_block(
        self,
        monkeypatch: pytest.MonkeyPatch,
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        # First call at block=1 populates cache; second call at block=2
        # invalidates it and re-fetches from rp.
        _patch_w3_eth(monkeypatch, block_number=1, eth_balance_wei=0)
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", 5 * 10**16)
        scripted_rp.set_call("rocketTokenRETH.getExchangeRate", 1 * ETH)
        scripted_rp.set_call("rocketNodeStaking.getNodeETHBonded", 0)
        scripted_rp.set_call("rocketNodeStaking.getNodeStakedRPL", 0)
        _patch_token_contracts_returning_zero(monkeypatch)

        await get_holding_for_address(ADDR)
        assert price_cache["block"] == 1
        assert price_cache["rpl_price"] == pytest.approx(0.05)

        # Bump the block; prices change.
        sc_module.w3.eth.get_block_number = AsyncMock(return_value=2)
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", 10 * 10**16)
        await get_holding_for_address(ADDR)
        assert price_cache["block"] == 2
        assert price_cache["rpl_price"] == pytest.approx(0.10)


class TestGetSeaCreatureForAddress:
    async def test_routes_through_holdings_to_emoji(
        self,
        monkeypatch: pytest.MonkeyPatch,
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        # Hand-craft a balance that lands in the 🦀 (64) tier.
        _patch_w3_eth(monkeypatch, block_number=1, eth_balance_wei=70 * ETH)
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", 0)
        scripted_rp.set_call("rocketTokenRETH.getExchangeRate", 0)
        scripted_rp.set_call("rocketNodeStaking.getNodeETHBonded", 0)
        scripted_rp.set_call("rocketNodeStaking.getNodeStakedRPL", 0)
        _patch_token_contracts_returning_zero(monkeypatch)

        result = await get_sea_creature_for_address(ADDR)
        assert result == sea_creatures[64]
