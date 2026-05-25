"""Tests for pure-math and pure-parsing paths in utils/liquidity.py.

Anything that touches chain/RPC is stubbed; we exercise the math directly via
the static methods.
"""

import math
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_typing import HexStr

from rocketwatch.utils import liquidity
from rocketwatch.utils.liquidity import (
    HTX,
    MEXC,
    OKX,
    BalancerV2,
    BalancerV3,
    Binance,
    BingX,
    Bitget,
    Bithumb,
    BitMart,
    Bitrue,
    Bitvavo,
    Bybit,
    Coinbase,
    CoinTR,
    CryptoDotCom,
    Curve,
    Deepcoin,
    DigiFinex,
    ERC20Token,
    GateIO,
    Kraken,
    Kucoin,
    Liquidity,
    Market,
    UniswapV3,
    UniswapV4,
)
from tests.lib.scripted_rocketpool import ScriptedRocketPool, addr

_ADDR0 = addr("0x" + "0" * 40)
_ADDR1 = addr("0x" + "1" * 40)
_ADDR2 = addr("0x" + "2" * 40)

# --- CEX order book parsing & depth_at --------------------------------------


class TestCEXOrderBookDepth:
    """End-to-end test that Binance's concrete CEX plumbing produces a
    Liquidity with correct mid and step-function depth_at."""

    @pytest.mark.asyncio
    async def test_depth_grows_monotonically_away_from_mid(self) -> None:
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
    async def test_empty_book_returns_none(self) -> None:
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
    def test_tick_price_roundtrip(self) -> None:
        for tick in [-100_000, -1000, 0, 1000, 71_134, 270_000]:
            price = UniswapV3.tick_to_price(tick)
            recovered = UniswapV3.price_to_tick(price)
            assert recovered == pytest.approx(tick, abs=1e-6)

    def test_tick_to_price_ordering(self) -> None:
        assert UniswapV3.tick_to_price(0) == pytest.approx(1.0)
        assert UniswapV3.tick_to_price(1) > 1.0
        assert UniswapV3.tick_to_price(-1) < 1.0

    def _make_pool(
        self, token_0_decimals: int, token_1_decimals: int, spacing: int = 60
    ) -> Any:
        """Build a V3 Pool instance without hitting chain."""
        pool: Any = UniswapV3.Pool.__new__(UniswapV3.Pool)
        pool.pool_address = _ADDR0
        pool.contract = MagicMock()
        pool.tick_spacing = spacing
        pool.token_0 = ERC20Token(_ADDR0, "T0", token_0_decimals)
        pool.token_1 = ERC20Token(_ADDR1, "T1", token_1_decimals)
        pool.primary_is_token_0 = False
        return pool

    def test_tick_to_word_and_bit(self) -> None:
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

    def test_liquidity_to_tokens_symmetric(self) -> None:
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

    def test_liquidity_to_tokens_scales_with_decimals(self) -> None:
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

    def test_invariant_matches_curve_equation_at_peg(self) -> None:
        """MetaStablePool now delegates to Curve's math; D satisfies
        ``Ann·S + D = Ann·D + D^3/(4·x0·x1)`` where Ann = amp·n (n=2)."""
        amp = 50.0
        x0 = x1 = 1000.0
        D = self.M._compute_invariant(amp, x0, x1)
        Ann = amp * 2
        lhs = Ann * (x0 + x1) + D
        rhs = Ann * D + D**3 / (4 * x0 * x1)
        assert lhs == pytest.approx(rhs, rel=1e-6)

    def test_invariant_matches_curve_equation_imbalanced(self) -> None:
        amp = 100.0
        x0, x1 = 1000.0, 1500.0
        D = self.M._compute_invariant(amp, x0, x1)
        Ann = amp * 2
        lhs = Ann * (x0 + x1) + D
        rhs = Ann * D + D**3 / (4 * x0 * x1)
        assert lhs == pytest.approx(rhs, rel=1e-6)

    def test_invariant_zero_sum_short_circuits(self) -> None:
        assert self.M._compute_invariant(50.0, 0.0, 0.0) == 0.0

    def test_balance_given_invariant_roundtrip(self) -> None:
        """Given (amp, x0, x1) and D, solving for x1 from (amp, D, x0) should
        return back x1."""
        amp = 50.0
        x0, x1 = 1200.0, 800.0
        D = self.M._compute_invariant(amp, x0, x1)
        solved_x1 = self.M._balance_given_invariant(amp, D, x0)
        assert solved_x1 == pytest.approx(x1, rel=1e-6)

    def test_balance_given_invariant_stable_for_large_b(self) -> None:
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

    def test_balance_given_invariant_infeasible_x_other(self) -> None:
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

    def test_spot_price_at_peg(self) -> None:
        """Symmetric balances → spot = 1."""
        amp = 50.0
        D = self.M._compute_invariant(amp, 1000.0, 1000.0)
        assert self.M._spot_price(amp, D, 1000.0, 1000.0) == pytest.approx(1.0)

    def test_spot_price_direction(self) -> None:
        """Curve's invariant gives spot monotonically *decreasing* in x0
        (more token_0 abundant → each dN0 buys less dN1), so x0 > x1 → spot < 1."""
        amp = 50.0
        D = self.M._compute_invariant(amp, 2000.0, 1000.0)
        assert self.M._spot_price(amp, D, 2000.0, 1000.0) < 1.0

    def test_swap_conservation(self) -> None:
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

    def test_invariant_matches_standard_curve_equation(self) -> None:
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

    def test_invariant_at_peg(self) -> None:
        """Symmetric balances with high A → D ≈ S."""
        A = 500.0
        x0 = x1 = 1000.0
        D = self.C._compute_invariant(A, x0, x1)
        # With very high A, stableswap approaches constant-sum → D → S.
        assert pytest.approx(2000.0, rel=1e-3) == D

    def test_invariant_zero_sum_short_circuits(self) -> None:
        assert self.C._compute_invariant(100.0, 0.0, 0.0) == 0.0

    def test_balance_given_invariant_roundtrip(self) -> None:
        A = 100.0
        x0, x1 = 1000.0, 1500.0
        D = self.C._compute_invariant(A, x0, x1)
        solved_x1 = self.C._balance_given_invariant(A, D, x0)
        assert solved_x1 == pytest.approx(x1, rel=1e-6)

    def test_spot_price_at_peg(self) -> None:
        A = 100.0
        D = self.C._compute_invariant(A, 1000.0, 1000.0)
        assert self.C._spot_price(A, D, 1000.0, 1000.0) == pytest.approx(1.0)

    def test_spot_price_direction(self) -> None:
        """More token_0 than token_1 → spot < 1 (each extra t0 buys less t1)."""
        A = 100.0
        D = self.C._compute_invariant(A, 2000.0, 1000.0)
        assert self.C._spot_price(A, D, 2000.0, 1000.0) < 1.0

    def test_swap_conservation(self) -> None:
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
    Curve uses 0xEEee…EEeE. The ``scripted_w3`` fixture makes
    ``to_checksum_address`` an identity so placeholder addresses pass through."""

    async def test_v4_zero_address_returns_eth(self, scripted_w3: MagicMock) -> None:
        token = await ERC20Token.create(addr("0x" + "0" * 40))
        assert token.symbol == "ETH"
        assert token.decimals == 18

    async def test_curve_ee_sentinel_returns_eth(self, scripted_w3: MagicMock) -> None:
        token = await ERC20Token.create(
            addr("0xEEeEeEeEeEeEeeEeEeEeEEEEeeeeEeeeeeeeEEeE")
        )
        assert token.symbol == "ETH"
        assert token.decimals == 18

    async def test_create_reads_symbol_and_decimals(
        self, scripted_rp: ScriptedRocketPool, scripted_w3: MagicMock
    ) -> None:
        scripted_rp.set_call("ERC20.symbol", "RPL")
        scripted_rp.set_call("ERC20.decimals", 18)
        token = await ERC20Token.create(_ADDR1)
        assert token.symbol == "RPL"
        assert token.decimals == 18
        assert str(token) == "RPL"
        assert _ADDR1 in repr(token)


# --- CEX order-book parsing across every exchange ---------------------------

ALL_CEX = [
    Binance,
    Coinbase,
    Deepcoin,
    GateIO,
    OKX,
    Bitget,
    MEXC,
    Bybit,
    CryptoDotCom,
    Kraken,
    Kucoin,
    Bithumb,
    BingX,
    Bitvavo,
    HTX,
    BitMart,
    Bitrue,
    CoinTR,
    DigiFinex,
]

# (cls, api_response, expected_bids, expected_asks). Each response mirrors the
# real shape that exchange's endpoint returns; expectations are the parsed
# {price: size} maps.
CEX_PARSE_CASES = [
    (
        Binance,
        {"bids": [["100", "1"], ["99", "2"]], "asks": [["101", "3"]]},
        {100.0: 1.0, 99.0: 2.0},
        {101.0: 3.0},
    ),
    (
        Coinbase,
        {
            "pricebook": {
                "bids": [{"price": "100", "size": "1"}],
                "asks": [{"price": "101", "size": "2"}],
            }
        },
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        Deepcoin,
        {"data": {"bids": [["100", "1"]], "asks": [["101", "2"]]}},
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        GateIO,
        {"bids": [["100", "1"]], "asks": [["101", "2"]]},
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        OKX,
        {
            "data": [
                {"bids": [["100", "1", "0", "1"]], "asks": [["101", "2", "0", "1"]]}
            ]
        },
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        Bitget,
        {"data": {"bids": [["100", "1"]], "asks": [["101", "2"]]}},
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        MEXC,
        {"bids": [["100", "1"]], "asks": [["101", "2"]]},
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        Bybit,
        {"result": {"b": [["100", "1"]], "a": [["101", "2"]]}},
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        CryptoDotCom,
        {
            "result": {
                "data": [{"bids": [["100", "1", "0"]], "asks": [["101", "2", "0"]]}]
            }
        },
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        Kraken,
        {
            "result": {
                "XRETHZUSD": {"bids": [["100", "1", "0"]], "asks": [["101", "2", "0"]]}
            }
        },
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        Kucoin,
        {"data": {"bids": [["100", "1"]], "asks": [["101", "2"]]}},
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        Bithumb,
        [
            {
                "orderbook_units": [
                    {
                        "bid_price": 100.0,
                        "bid_size": 1.0,
                        "ask_price": 101.0,
                        "ask_size": 2.0,
                    }
                ]
            }
        ],
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        BingX,
        {"data": {"bids": [["100", "1"]], "asks": [["101", "2"]]}},
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        Bitvavo,
        {"bids": [["100", "1"]], "asks": [["101", "2"]]},
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        HTX,
        {"tick": {"bids": [["100", "1"]], "asks": [["101", "2"]]}},
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        BitMart,
        {"data": {"bids": [["100", "1"]], "asks": [["101", "2"]]}},
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        Bitrue,
        {"data": {"tick": {"b": [["100", "1"]], "a": [["101", "2"]]}}},
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        CoinTR,
        {"data": {"bids": [["100", "1"]], "asks": [["101", "2"]]}},
        {100.0: 1.0},
        {101.0: 2.0},
    ),
    (
        DigiFinex,
        {"bids": [[100.0, 1.0]], "asks": [[101.0, 2.0]]},
        {100.0: 1.0},
        {101.0: 2.0},
    ),
]


class TestCEXParsing:
    @pytest.mark.parametrize("cls", ALL_CEX)
    def test_metadata_is_well_formed(self, cls: Any) -> None:
        cex = cls("RETH", ["WETH"])
        market = next(iter(cex.markets))
        assert cex.color.startswith("#")
        assert cex._api_base_url.startswith("http")
        assert cls._get_request_path(market)
        assert cls._get_request_params(market)
        assert str(cex)

    @pytest.mark.parametrize(
        "cls,response,exp_bids,exp_asks",
        CEX_PARSE_CASES,
        ids=[c[0].__name__ for c in CEX_PARSE_CASES],
    )
    def test_bids_and_asks_parse(
        self, cls: Any, response: Any, exp_bids: Any, exp_asks: Any
    ) -> None:
        cex = cls("RETH", ["WETH"])
        assert cex._get_bids(response) == exp_bids
        assert cex._get_asks(response) == exp_asks

    def test_cryptodotcom_display_name(self) -> None:
        assert str(CryptoDotCom("RETH", ["WETH"])) == "Crypto.com"

    async def test_get_liquidity_collects_nonempty_markets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cex = Binance("RPL", ["USDT", "USD"])
        liq = Liquidity(1.0, lambda _p: 0.0)

        async def scripted_get_liquidity(
            market: Any, _session: Any
        ) -> Liquidity | None:
            return liq if market.minor == "USDT" else None

        monkeypatch.setattr(cex, "_get_liquidity", scripted_get_liquidity)
        result = await cex.get_liquidity(MagicMock())
        assert result == {Market("RPL", "USDT"): liq}


# --- DEX top-level wrappers --------------------------------------------------


class TestDEXTopLevel:
    @pytest.mark.parametrize(
        "cls,name",
        [
            (BalancerV2, "Balancer V2"),
            (BalancerV3, "Balancer V3"),
            (Curve, "Curve"),
            (UniswapV3, "Uniswap V3"),
            (UniswapV4, "Uniswap V4"),
        ],
    )
    def test_str_and_color(self, cls: Any, name: str) -> None:
        dex = cls([])
        assert str(dex) == name
        assert dex.color.startswith("#")

    async def test_get_liquidity_skips_empty_pools(self) -> None:
        liq = Liquidity(1.0, lambda _p: 0.0)
        full = MagicMock()
        full.get_liquidity = AsyncMock(return_value=liq)
        empty = MagicMock()
        empty.get_liquidity = AsyncMock(return_value=None)
        dex = Curve([full, empty])
        result = await dex.get_liquidity()
        assert result == {full: liq}


# --- Balancer V2 WeightedPool (constant-product depth) ----------------------


def _weighted_pool(b0: int, b1: int, dec0: int = 18, dec1: int = 18) -> Any:
    pool: Any = BalancerV2.WeightedPool.__new__(BalancerV2.WeightedPool)
    pool.id = "0x" + "0" * 64
    pool.token_0 = ERC20Token(_ADDR0, "T0", dec0)
    pool.token_1 = ERC20Token(_ADDR1, "T1", dec1)
    vault = MagicMock()
    vault.functions.getPoolTokens.return_value.call = AsyncMock(
        return_value=(["t0", "t1"], [b0, b1])
    )
    pool.vault = vault
    return pool


class TestWeightedPool:
    async def test_get_price(self) -> None:
        pool = _weighted_pool(1000 * 10**18, 2000 * 10**18)
        assert await pool.get_price() == pytest.approx(2.0)

    async def test_get_price_zero_balance_is_zero(self) -> None:
        pool = _weighted_pool(0, 2000 * 10**18)
        assert await pool.get_price() == 0

    async def test_get_normalized_price_applies_decimals(self) -> None:
        pool = _weighted_pool(1000 * 10**6, 2000 * 10**18, dec0=6, dec1=18)
        raw = await pool.get_price()
        assert await pool.get_normalized_price() == pytest.approx(raw * 10 ** (6 - 18))

    async def test_get_liquidity_depth_grows_away_from_price(self) -> None:
        pool = _weighted_pool(1000 * 10**18, 1000 * 10**18)
        liq = await pool.get_liquidity()
        assert liq is not None
        assert liq.price > 0
        near = liq.depth_at(liq.price * 1.01)
        far = liq.depth_at(liq.price * 1.10)
        assert far > 0
        assert 0 <= near <= far

    async def test_get_liquidity_empty_returns_none(self) -> None:
        pool = _weighted_pool(0, 1000 * 10**18)
        assert await pool.get_liquidity() is None

    async def test_create(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripted_rp.set_call("BalancerV2Vault.getPoolTokens", ([_ADDR0, _ADDR1], None))
        t0 = ERC20Token(_ADDR0, "rETH", 18)
        t1 = ERC20Token(_ADDR1, "WETH", 18)
        monkeypatch.setattr(
            liquidity.ERC20Token, "create", AsyncMock(side_effect=[t0, t1])
        )
        pool = await BalancerV2.WeightedPool.create(HexStr("0xpoolid"))
        assert pool.id == "0xpoolid"
        assert pool.token_0 is t0
        assert pool.token_1 is t1


# --- Balancer V2 MetaStablePool (stableswap depth + rates) ------------------


def _metastable_pool(
    state: tuple[float, float, float, float, float], primary_0: bool = False
) -> Any:
    pool: Any = BalancerV2.MetaStablePool.__new__(BalancerV2.MetaStablePool)
    pool.token_0 = ERC20Token(_ADDR0, "T0", 18)
    pool.token_1 = ERC20Token(_ADDR1, "T1", 18)
    pool.primary_is_token_0 = primary_0
    pool._get_state = AsyncMock(return_value=state)
    return pool


class TestMetaStablePool:
    async def test_get_price_balanced_is_near_one(self) -> None:
        pool = _metastable_pool((1000.0, 1000.0, 50.0, 1.0, 1.0))
        assert await pool.get_price() == pytest.approx(1.0, rel=1e-3)

    async def test_get_price_scales_by_rates(self) -> None:
        pool = _metastable_pool((1000.0, 1000.0, 50.0, 1.1, 1.0))
        assert await pool.get_price() == pytest.approx(1.1, rel=1e-3)

    async def test_get_normalized_price_equals_price(self) -> None:
        pool = _metastable_pool((1000.0, 1200.0, 50.0, 1.0, 1.0))
        assert await pool.get_normalized_price() == await pool.get_price()

    @pytest.mark.parametrize("primary_0", [True, False])
    async def test_get_liquidity_depth(self, primary_0: bool) -> None:
        pool = _metastable_pool((1000.0, 1000.0, 50.0, 1.0, 1.0), primary_0=primary_0)
        liq = await pool.get_liquidity()
        assert liq is not None
        assert liq.depth_at(liq.price) == 0.0
        assert liq.depth_at(-1.0) == 0.0
        assert liq.depth_at(0.0) == 0.0
        near = liq.depth_at(liq.price * 1.02)
        far = liq.depth_at(liq.price * 1.10)
        assert far > 0
        assert 0 <= near <= far

    async def test_get_liquidity_empty_returns_none(self) -> None:
        pool = _metastable_pool((0.0, 1000.0, 50.0, 1.0, 1.0))
        assert await pool.get_liquidity() is None

    @pytest.mark.parametrize("with_rates", [True, False])
    async def test_get_state_reads_vault_and_rates(
        self, scripted_rp: ScriptedRocketPool, with_rates: bool
    ) -> None:
        pool: Any = BalancerV2.MetaStablePool.__new__(BalancerV2.MetaStablePool)
        pool.id = "0x" + "0" * 64
        pool.token_0 = ERC20Token(_ADDR0, "T0", 18)
        pool.token_1 = ERC20Token(_ADDR1, "T1", 18)
        vault = MagicMock()
        vault.functions.getPoolTokens.return_value.call = AsyncMock(
            return_value=(["t0", "t1"], [1000 * 10**18, 1100 * 10**18], 0)
        )
        pool.vault = vault
        pool_contract = MagicMock()
        pool_contract.functions.getAmplificationParameter.return_value.call = AsyncMock(
            return_value=(5000, False, 1000)
        )
        pool.pool_contract = pool_contract
        pool.rate_fn_0 = AsyncMock(return_value=1.05) if with_rates else None
        pool.rate_fn_1 = AsyncMock(return_value=1.0) if with_rates else None

        n0, n1, amp, r0, r1 = await pool._get_state()
        assert amp == pytest.approx(5.0)
        assert (r0, r1) == pytest.approx((1.05, 1.0) if with_rates else (1.0, 1.0))
        assert n0 == pytest.approx(1000 * r0)
        assert n1 == pytest.approx(1100 * r1)

    async def test_create(
        self,
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scripted_rp.set_call("BalancerV2Vault.getPoolTokens", ([_ADDR0, _ADDR1], None))
        t0 = ERC20Token(_ADDR0, "rETH", 18)
        t1 = ERC20Token(_ADDR1, "WETH", 18)
        monkeypatch.setattr(
            liquidity.ERC20Token, "create", AsyncMock(side_effect=[t0, t1])
        )
        pool = await BalancerV2.MetaStablePool.create(
            HexStr("0x" + "ab" * 32), primary_is_token_0=True
        )
        assert pool.token_0 is t0
        assert pool.token_1 is t1
        assert pool.primary_is_token_0 is True


# --- Balancer V3 StablePool (rate derived from raw vs live balances) --------


class TestBalancerV3StablePool:
    @pytest.mark.parametrize("raw0_zero", [False, True])
    async def test_get_state_derives_rates(
        self, scripted_rp: ScriptedRocketPool, raw0_zero: bool
    ) -> None:
        pool = BalancerV3.StablePool.__new__(BalancerV3.StablePool)
        pool.pool_address = _ADDR0
        pool.token_0 = ERC20Token(_ADDR0, "T0", 18)
        pool.token_1 = ERC20Token(_ADDR1, "T1", 6)
        raw0 = 0 if raw0_zero else 1000 * 10**18
        raw1 = 2000 * 10**6
        live0 = 1050 * 10**18
        live1 = 2000 * 10**18
        vault = MagicMock()
        vault.functions.getPoolTokenInfo.return_value.call = AsyncMock(
            return_value=(["t0", "t1"], None, [raw0, raw1], [live0, live1])
        )
        pool.vault = vault
        pool_contract = MagicMock()
        pool_contract.functions.getAmplificationParameter.return_value.call = AsyncMock(
            return_value=(2000, False, 1000)
        )
        pool.pool_contract = pool_contract

        n0, n1, amp, r0, r1 = await pool._get_state()
        assert amp == pytest.approx(2.0)
        assert r1 == pytest.approx(1.0)
        assert r0 == pytest.approx(1.0 if raw0_zero else 1.05)
        assert n0 == pytest.approx(1050.0)
        assert n1 == pytest.approx(2000.0)

    async def test_create(
        self,
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scripted_rp.set_call(
            "BalancerV3Vault.getPoolTokenInfo", ([_ADDR0, _ADDR1], None, None, None)
        )
        t0 = ERC20Token(_ADDR0, "rETH", 18)
        t1 = ERC20Token(_ADDR1, "WETH", 18)
        monkeypatch.setattr(
            liquidity.ERC20Token, "create", AsyncMock(side_effect=[t0, t1])
        )
        pool = await BalancerV3.StablePool.create(_ADDR2, primary_is_token_0=True)
        assert pool.token_0 is t0
        assert pool.primary_is_token_0 is True


# --- Curve StablePool (A·n^n stableswap depth) ------------------------------


def _curve_pool(state: tuple[float, float, float], primary_0: bool = False) -> Any:
    pool: Any = Curve.StablePool.__new__(Curve.StablePool)
    pool.token_0 = ERC20Token(_ADDR0, "T0", 18)
    pool.token_1 = ERC20Token(_ADDR1, "T1", 18)
    pool.primary_is_token_0 = primary_0
    pool._get_state = AsyncMock(return_value=state)
    return pool


class TestCurveStablePool:
    async def test_get_price_balanced(self) -> None:
        pool = _curve_pool((1000.0, 1000.0, 100.0))
        assert await pool.get_price() == pytest.approx(1.0, rel=1e-3)

    async def test_get_normalized_price_equals_price(self) -> None:
        pool = _curve_pool((1000.0, 1200.0, 100.0))
        assert await pool.get_normalized_price() == await pool.get_price()

    @pytest.mark.parametrize("primary_0", [True, False])
    async def test_get_liquidity_depth(self, primary_0: bool) -> None:
        pool = _curve_pool((1000.0, 1000.0, 100.0), primary_0=primary_0)
        liq = await pool.get_liquidity()
        assert liq is not None
        assert liq.depth_at(liq.price) == 0.0
        assert liq.depth_at(-1.0) == 0.0
        near = liq.depth_at(liq.price * 1.02)
        far = liq.depth_at(liq.price * 1.10)
        assert far > 0
        assert 0 <= near <= far

    async def test_get_liquidity_empty_returns_none(self) -> None:
        pool = _curve_pool((0.0, 1000.0, 100.0))
        assert await pool.get_liquidity() is None

    async def test_get_state(self, scripted_rp: ScriptedRocketPool) -> None:
        pool: Any = Curve.StablePool.__new__(Curve.StablePool)
        pool.token_0 = ERC20Token(_ADDR0, "T0", 18)
        pool.token_1 = ERC20Token(_ADDR1, "T1", 6)
        pool.contract = scripted_rp.contract_at(_ADDR2)
        scripted_rp.set_call(
            f"{_ADDR2}.balances", lambda i: [1000 * 10**18, 2000 * 10**6][i]
        )
        scripted_rp.set_call(f"{_ADDR2}.A", 100)
        x0, x1, a = await pool._get_state()
        assert (x0, x1, a) == pytest.approx((1000.0, 2000.0, 100.0))

    async def test_create(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripted_rp.set_call("curvePool.coins", lambda i: [_ADDR0, _ADDR1][i])
        t0 = ERC20Token(_ADDR0, "rETH", 18)
        t1 = ERC20Token(_ADDR1, "WETH", 18)
        monkeypatch.setattr(
            liquidity.ERC20Token, "create", AsyncMock(side_effect=[t0, t1])
        )
        pool = await Curve.StablePool.create(_ADDR2, primary_is_token_0=True)
        assert pool.token_0 is t0
        assert pool.token_1 is t1
        assert pool.primary_is_token_0 is True


# --- Uniswap V3 concentrated-liquidity pool ---------------------------------


def _univ3_pool(
    dec0: int = 18, dec1: int = 18, spacing: int = 10, primary_0: bool = False
) -> Any:
    pool: Any = UniswapV3.Pool.__new__(UniswapV3.Pool)
    pool.pool_address = _ADDR0
    pool.contract = MagicMock()
    pool.tick_spacing = spacing
    pool.token_0 = ERC20Token(_ADDR0, "T0", dec0)
    pool.token_1 = ERC20Token(_ADDR1, "T1", dec1)
    pool.primary_is_token_0 = primary_0
    return pool


def _scripted_call(value: Any) -> MagicMock:
    call = MagicMock()
    call.call = AsyncMock(return_value=value)
    return call


class TestUniswapV3Pool:
    async def test_get_price(self) -> None:
        pool = _univ3_pool()
        pool._fn_slot0 = MagicMock(return_value=_scripted_call((2**96, 0, 0)))
        assert await pool.get_price() == pytest.approx(1.0)

    async def test_get_normalized_price_applies_decimals(self) -> None:
        pool = _univ3_pool(dec0=6, dec1=18)
        pool._fn_slot0 = MagicMock(return_value=_scripted_call((2**96,)))
        raw = await pool.get_price()
        assert await pool.get_normalized_price() == pytest.approx(raw * 10 ** (6 - 18))

    async def test_get_liquidity_depth_two_sided(self) -> None:
        pool = _univ3_pool(spacing=10)
        pool._fn_slot0 = MagicMock(return_value=_scripted_call((2**96,)))
        pool._fn_liquidity = MagicMock(return_value=_scripted_call(10**18))
        ticks = [-200, -100, 100, 200]
        pool.get_initialized_ticks = AsyncMock(return_value=ticks)
        pool.get_ticks_net_liquidity = AsyncMock(
            return_value={t: 10**17 for t in ticks}
        )
        liq = await pool.get_liquidity()
        assert liq is not None
        assert liq.price > 0
        assert liq.depth_at(liq.price) == pytest.approx(0.0, abs=1e-9)
        # moving up crosses asks, moving down crosses bids; both add depth
        assert liq.depth_at(liq.price * 1.01) > 0
        assert liq.depth_at(liq.price * 0.99) > 0
        # far past the book the depth saturates (finite)
        assert math.isfinite(liq.depth_at(0.0))

    async def test_get_liquidity_depth_primary_token_0(self) -> None:
        pool = _univ3_pool(spacing=10, primary_0=True)
        pool._fn_slot0 = MagicMock(return_value=_scripted_call((2**96,)))
        pool._fn_liquidity = MagicMock(return_value=_scripted_call(10**18))
        ticks = [-200, -100, 100, 200]
        pool.get_initialized_ticks = AsyncMock(return_value=ticks)
        pool.get_ticks_net_liquidity = AsyncMock(
            return_value={t: 10**17 for t in ticks}
        )
        liq = await pool.get_liquidity()
        assert liq is not None
        assert liq.depth_at(liq.price) == pytest.approx(0.0, abs=1e-9)
        # 0.95 walks several ask ticks before the target; 1.05 walks the bids
        assert liq.depth_at(liq.price * 0.95) > 0
        assert liq.depth_at(liq.price * 1.05) > 0
        assert math.isfinite(liq.depth_at(0.0))

    async def test_get_liquidity_no_ticks_returns_none(self) -> None:
        pool = _univ3_pool()
        pool._fn_slot0 = MagicMock(return_value=_scripted_call((2**96,)))
        pool._fn_liquidity = MagicMock(return_value=_scripted_call(10**18))
        pool.get_initialized_ticks = AsyncMock(return_value=[])
        assert await pool.get_liquidity() is None

    async def test_fn_accessors_route_through_contract(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        pool = _univ3_pool()
        pool.contract = scripted_rp.contract_at(_ADDR2)
        scripted_rp.set_call(f"{_ADDR2}.slot0", (2**96,))
        scripted_rp.set_call(f"{_ADDR2}.liquidity", 10**18)
        assert (await pool._fn_slot0().call())[0] == 2**96
        assert await pool._fn_liquidity().call() == 10**18

    async def test_get_ticks_net_liquidity(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        pool = _univ3_pool()
        pool.contract = scripted_rp.contract_at(_ADDR2)
        scripted_rp.set_call(f"{_ADDR2}.ticks", lambda t: (0, t * 10, 0))
        result = await pool.get_ticks_net_liquidity([10, 20, 30])
        assert result == {10: 100, 20: 200, 30: 300}

    async def test_get_initialized_ticks(self, scripted_rp: ScriptedRocketPool) -> None:
        pool = _univ3_pool(spacing=10)
        pool.contract = scripted_rp.contract_at(_ADDR2)
        bitmap = (1 << 0) | (1 << 5)
        scripted_rp.set_call(f"{_ADDR2}.tickBitmap", lambda w: bitmap if w == 0 else 0)
        ticks = await pool.get_initialized_ticks(0)
        assert 0 in ticks
        assert 50 in ticks

    async def test_create(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripted_rp.set_call("UniswapV3Pool.tickSpacing", 10)
        scripted_rp.set_call("UniswapV3Pool.token0", _ADDR0)
        scripted_rp.set_call("UniswapV3Pool.token1", _ADDR1)
        t0 = ERC20Token(_ADDR0, "rETH", 18)
        t1 = ERC20Token(_ADDR1, "WETH", 18)
        monkeypatch.setattr(
            liquidity.ERC20Token, "create", AsyncMock(side_effect=[t0, t1])
        )
        pool = await UniswapV3.Pool.create(_ADDR2, primary_is_token_0=True)
        assert pool.tick_spacing == 10
        assert pool.token_0 is t0
        assert pool.primary_is_token_0 is True

    async def test_uniswap_v3_create_builds_pools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripted_pool = MagicMock()
        monkeypatch.setattr(
            liquidity.UniswapV3.Pool,
            "create",
            AsyncMock(return_value=scripted_pool),
        )
        dex = await UniswapV3.create([_ADDR0, _ADDR1])
        assert dex.pools == [scripted_pool, scripted_pool]


# --- Uniswap V4 (StateView-backed reads) ------------------------------------


class TestUniswapV4Pool:
    async def test_create_and_fn_overrides(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        t0 = ERC20Token(_ADDR0, "ETH", 18)
        t1 = ERC20Token(_ADDR1, "rETH", 18)
        monkeypatch.setattr(
            liquidity.ERC20Token, "create", AsyncMock(side_effect=[t0, t1])
        )
        pool = await UniswapV4.Pool.create(HexStr("0xpoolid"), 10, _ADDR0, _ADDR1)
        assert pool.tick_spacing == 10
        assert pool.primary_is_token_0 is False
        assert pool._fn_slot0() is not None
        assert pool._fn_liquidity() is not None
        assert pool._fn_ticks(0) is not None
        assert pool._fn_tick_bitmap(0) is not None

    async def test_uniswap_v4_create_builds_pools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripted_pool = MagicMock()
        monkeypatch.setattr(
            liquidity.UniswapV4.Pool,
            "create",
            AsyncMock(return_value=scripted_pool),
        )
        dex = await UniswapV4.create([(HexStr("0xid"), 10, _ADDR0, _ADDR1)])
        assert dex.pools == [scripted_pool]
