"""Tests for pure-math and pure-parsing paths in utils/liquidity.py.

Anything that touches chain/RPC is stubbed; we exercise the math directly via
the static methods.
"""

import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from rocketwatch.utils.liquidity import (
    BalancerV2,
    Binance,
    Curve,
    ERC20Token,
    UniswapV3,
)

# --- CEX order book parsing & depth_at --------------------------------------


class TestCEXOrderBookDepth:
    """End-to-end test that Binance's concrete CEX plumbing produces a
    Liquidity with correct mid and step-function depth_at."""

    @pytest.mark.asyncio
    async def test_depth_grows_monotonically_away_from_mid(self):
        fake_response = AsyncMock()
        fake_response.json = AsyncMock(
            return_value={
                # Each entry is [price, size]
                "bids": [
                    ["100.00", "1.0"],
                    ["99.00", "2.0"],
                    ["98.00", "3.0"],
                ],
                "asks": [
                    ["101.00", "1.0"],
                    ["102.00", "2.0"],
                    ["103.00", "3.0"],
                ],
            }
        )
        session = MagicMock()
        session.get = AsyncMock(return_value=fake_response)

        cex = Binance("RPL", ["USDT"])
        market = next(iter(cex.markets))
        liq = await cex._get_liquidity(market, session)

        assert liq is not None
        # Mid is (100 + 101) / 2 = 100.5
        assert liq.price == pytest.approx(100.5)

        # Inside the spread: no depth available.
        assert liq.depth_at(100.5) == 0

        # Bid side: cumulative (price * size) summed as we widen the range.
        # At exactly $100 we cross only the $100 bid → 100 * 1 = 100.
        assert liq.depth_at(100.0) == pytest.approx(100.0)
        # At $99 we've crossed both top bids → 100 + 99*2 = 298.
        assert liq.depth_at(99.0) == pytest.approx(298.0)
        # Beyond the lowest bid we stay at the total bid liquidity.
        assert liq.depth_at(50.0) == pytest.approx(298.0 + 98.0 * 3)

        # Ask side mirrors.
        assert liq.depth_at(101.0) == pytest.approx(101.0)
        assert liq.depth_at(102.0) == pytest.approx(101.0 + 102.0 * 2)
        assert liq.depth_at(1_000.0) == pytest.approx(101.0 + 102.0 * 2 + 103.0 * 3)

    @pytest.mark.asyncio
    async def test_empty_book_returns_none(self):
        fake_response = AsyncMock()
        fake_response.json = AsyncMock(return_value={"bids": [], "asks": []})
        session = MagicMock()
        session.get = AsyncMock(return_value=fake_response)

        cex = Binance("RPL", ["USDT"])
        market = next(iter(cex.markets))
        liq = await cex._get_liquidity(market, session)
        assert liq is None


# --- Uniswap V3 math --------------------------------------------------------


class TestUniswapV3Math:
    def test_tick_price_roundtrip(self):
        for tick in [-100_000, -1000, 0, 1000, 71_134, 270_000]:
            price = UniswapV3.tick_to_price(tick)
            recovered = UniswapV3.price_to_tick(price)
            assert recovered == pytest.approx(tick, abs=1e-6)

    def test_tick_to_price_ordering(self):
        assert UniswapV3.tick_to_price(0) == pytest.approx(1.0)
        assert UniswapV3.tick_to_price(1) > 1.0
        assert UniswapV3.tick_to_price(-1) < 1.0

    def _make_pool(
        self, token_0_decimals: int, token_1_decimals: int, spacing: int = 60
    ):
        """Build a V3 Pool instance without hitting chain."""
        pool = UniswapV3.Pool.__new__(UniswapV3.Pool)
        pool.pool_address = "0x" + "0" * 40
        pool.contract = MagicMock()
        pool.tick_spacing = spacing
        pool.token_0 = ERC20Token("0x" + "0" * 40, "T0", token_0_decimals)
        pool.token_1 = ERC20Token("0x" + "1" * 40, "T1", token_1_decimals)
        pool.primary_is_token_0 = False
        return pool

    def test_tick_to_word_and_bit(self):
        pool = self._make_pool(18, 18, spacing=60)
        # tick 120 → compressed = 2 → word 0 bit 2
        word, bit = pool.tick_to_word_and_bit(120)
        assert (word, bit) == (0, 2)
        # tick -60 → compressed -1 → word -1 bit 255
        word, bit = pool.tick_to_word_and_bit(-60)
        assert (word, bit) == (-1, 255)
        # tick that's not a multiple of spacing: -61 → floor div by 60 = -2,
        # then compressed -= 1 because negative-and-not-divisible → -3.
        # word = -3//256 = -1, bit = -3%256 = 253.
        word, bit = pool.tick_to_word_and_bit(-61)
        assert (word, bit) == (-1, 253)

    def test_liquidity_to_tokens_symmetric(self):
        """At tick 0 both sqrt prices equal 1, so delta_x == delta_y and
        splitting a unit of L above current tick produces (0, L/10^dec)."""
        pool = self._make_pool(18, 18)
        L = 10**20
        # Range [0, 100]: sqrtp_lo = 1.0, sqrtp_hi = sqrt(1.0001^100) ≈ 1.005.
        balance_0, balance_1 = pool.liquidity_to_tokens(L, 0, 100)
        # delta_x positive, delta_y positive.
        assert balance_0 > 0
        assert balance_1 > 0
        # Symmetry check: the range is small so delta_x ≈ delta_y.
        assert balance_0 == pytest.approx(balance_1, rel=0.01)

    def test_liquidity_to_tokens_scales_with_decimals(self):
        """Same L/tick range, different token_0 decimals → balance_0 scales."""
        pool_18 = self._make_pool(18, 18)
        pool_6 = self._make_pool(6, 18)
        L = 10**20
        b0_18, _ = pool_18.liquidity_to_tokens(L, 0, 100)
        b0_6, _ = pool_6.liquidity_to_tokens(L, 0, 100)
        # token_0 with 6 decimals means we divide delta_x by 1e6 instead of 1e18
        # → 10^12 larger "human" balance.
        assert b0_6 == pytest.approx(b0_18 * 10**12, rel=1e-9)


# --- Stableswap math (Balancer V2 MetaStable / V3 StablePool) ---------------


class TestStableswapMath:
    """The static math methods on MetaStablePool are shared by V2 + V3."""

    M = BalancerV2.MetaStablePool

    def test_invariant_matches_curve_equation_at_peg(self):
        """MetaStablePool now delegates to Curve's math; D satisfies
        ``Ann·S + D = Ann·D + D^3/(4·x0·x1)`` where Ann = amp·n (n=2)."""
        amp = 50.0
        x0 = x1 = 1000.0
        D = self.M._compute_invariant(amp, x0, x1)
        Ann = amp * 2
        lhs = Ann * (x0 + x1) + D
        rhs = Ann * D + D**3 / (4 * x0 * x1)
        assert lhs == pytest.approx(rhs, rel=1e-6)

    def test_invariant_matches_curve_equation_imbalanced(self):
        amp = 100.0
        x0, x1 = 1000.0, 1500.0
        D = self.M._compute_invariant(amp, x0, x1)
        Ann = amp * 2
        lhs = Ann * (x0 + x1) + D
        rhs = Ann * D + D**3 / (4 * x0 * x1)
        assert lhs == pytest.approx(rhs, rel=1e-6)

    def test_invariant_zero_sum_short_circuits(self):
        assert self.M._compute_invariant(50.0, 0.0, 0.0) == 0.0

    def test_balance_given_invariant_roundtrip(self):
        """Given (amp, x0, x1) and D, solving for x1 from (amp, D, x0) should
        return back x1."""
        amp = 50.0
        x0, x1 = 1200.0, 800.0
        D = self.M._compute_invariant(amp, x0, x1)
        solved_x1 = self.M._balance_given_invariant(amp, D, x0)
        assert solved_x1 == pytest.approx(x1, rel=1e-6)

    def test_balance_given_invariant_stable_for_large_b(self):
        """Regression: the rationalized quadratic form avoids catastrophic
        cancellation when `b` is a large positive number."""
        amp = 50.0
        D = 1e20
        # Very large x_other such that b = 4 x_other (4A x_other + D - 4AD)
        # is large and positive.
        x_other = 1e15
        result = self.M._balance_given_invariant(amp, D, x_other)
        # Should be a tiny positive number, not 0 or NaN.
        assert result > 0
        assert math.isfinite(result)

    def test_balance_given_invariant_infeasible_x_other(self):
        """Regression: if bisection probes x_other far beyond D (infeasible
        region), return a finite positive value instead of ~0, which would
        otherwise trip ZeroDivisionError in `_spot_price`."""
        amp = 61.7
        x0, x1 = 1236.0, 1198.0
        D = self.M._compute_invariant(amp, x0, x1)
        # x_other = N0 * 1e6 is the old (buggy) bisection upper bound.
        huge = x0 * 1e6
        result = self.M._balance_given_invariant(amp, D, huge)
        assert result > 0
        assert math.isfinite(result)
        # And _spot_price should not crash on the returned pair.
        spot = self.M._spot_price(amp, D, huge, result)
        assert math.isfinite(spot)

    def test_spot_price_at_peg(self):
        """Symmetric balances → spot = 1."""
        amp = 50.0
        D = self.M._compute_invariant(amp, 1000.0, 1000.0)
        assert self.M._spot_price(amp, D, 1000.0, 1000.0) == pytest.approx(1.0)

    def test_spot_price_direction(self):
        """Curve's invariant gives spot monotonically *decreasing* in x0
        (more token_0 abundant → each dN0 buys less dN1), so x0 > x1 → spot < 1."""
        amp = 50.0
        D = self.M._compute_invariant(amp, 2000.0, 1000.0)
        assert self.M._spot_price(amp, D, 2000.0, 1000.0) < 1.0

    def test_swap_conservation(self):
        """After an infinitesimal swap, the invariant is preserved: given a
        small dx0, the solved x1 keeps D within numerical tolerance."""
        amp = 50.0
        x0, x1 = 1000.0, 1200.0
        D = self.M._compute_invariant(amp, x0, x1)

        new_x0 = x0 * 1.01
        new_x1 = self.M._balance_given_invariant(amp, D, new_x0)
        new_D = self.M._compute_invariant(amp, new_x0, new_x1)
        assert new_D == pytest.approx(D, rel=1e-6)


# --- Curve stableswap math (A·n^n form) -------------------------------------


class TestCurveStableswapMath:
    """Curve's classic stableswap uses a different invariant form than
    Balancer's (A·n^n vs A·n). The two classes have separate static math."""

    C = Curve.StablePool

    def test_invariant_matches_standard_curve_equation(self):
        """D satisfies Curve's invariant ``Ann·S + D = Ann·D + D_P`` where
        ``Ann = amp·n`` (amp is Curve's contract-level A, scaled by n^(n-1))
        and ``D_P = D^(n+1)/(n^n·prod)``."""
        A = 100.0
        x0, x1 = 1000.0, 1200.0
        D = self.C._compute_invariant(A, x0, x1)
        Ann = A * 2  # n=2 → Ann = amp * n
        lhs = Ann * (x0 + x1) + D
        rhs = Ann * D + D**3 / (4 * x0 * x1)
        assert lhs == pytest.approx(rhs, rel=1e-6)

    def test_invariant_at_peg(self):
        """Symmetric balances with high A → D ≈ S."""
        A = 500.0
        x0 = x1 = 1000.0
        D = self.C._compute_invariant(A, x0, x1)
        # With very high A, stableswap approaches constant-sum → D → S.
        assert pytest.approx(2000.0, rel=1e-3) == D

    def test_invariant_zero_sum_short_circuits(self):
        assert self.C._compute_invariant(100.0, 0.0, 0.0) == 0.0

    def test_balance_given_invariant_roundtrip(self):
        A = 100.0
        x0, x1 = 1000.0, 1500.0
        D = self.C._compute_invariant(A, x0, x1)
        solved_x1 = self.C._balance_given_invariant(A, D, x0)
        assert solved_x1 == pytest.approx(x1, rel=1e-6)

    def test_spot_price_at_peg(self):
        A = 100.0
        D = self.C._compute_invariant(A, 1000.0, 1000.0)
        assert self.C._spot_price(A, D, 1000.0, 1000.0) == pytest.approx(1.0)

    def test_spot_price_direction(self):
        """More token_0 than token_1 → spot < 1 (each extra t0 buys less t1)."""
        A = 100.0
        D = self.C._compute_invariant(A, 2000.0, 1000.0)
        assert self.C._spot_price(A, D, 2000.0, 1000.0) < 1.0

    def test_swap_conservation(self):
        A = 100.0
        x0, x1 = 1000.0, 1200.0
        D = self.C._compute_invariant(A, x0, x1)
        new_x0 = x0 * 1.01
        new_x1 = self.C._balance_given_invariant(A, D, new_x0)
        new_D = self.C._compute_invariant(A, new_x0, new_x1)
        assert new_D == pytest.approx(D, rel=1e-6)


# --- ERC20Token native-ETH sentinels ----------------------------------------


class TestERC20TokenEthSentinels:
    """Native-ETH short-circuits in ERC20Token.create — Uniswap V4 uses 0x0,
    Curve uses 0xEEee…EEeE. w3.to_checksum_address is mocked in conftest, so
    patch it to pass the address through unchanged."""

    @pytest.fixture(autouse=True)
    def _passthrough_checksum(self, monkeypatch):
        from rocketwatch.utils import liquidity

        monkeypatch.setattr(liquidity.w3, "to_checksum_address", lambda a: a)

    @pytest.mark.asyncio
    async def test_v4_zero_address_returns_eth(self):
        token = await ERC20Token.create("0x0000000000000000000000000000000000000000")
        assert token.symbol == "ETH"
        assert token.decimals == 18

    @pytest.mark.asyncio
    async def test_curve_ee_sentinel_returns_eth(self):
        token = await ERC20Token.create("0xEEeEeEeEeEeEeeEeEeEeEEEEeeeeEeeeeeeeEEeE")
        assert token.symbol == "ETH"
        assert token.decimals == 18
