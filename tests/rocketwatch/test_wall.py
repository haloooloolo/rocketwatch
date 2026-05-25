"""Tests for plugins/wall/wall.py."""

from collections import OrderedDict
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import numpy as np
import pytest
from matplotlib import figure
from matplotlib import pyplot as plt

from rocketwatch.plugins.wall.wall import MarketConfig, Wall
from tests.lib.discord_harness import make_bot, make_interaction

# --- Tick formatter ---------------------------------------------------------


class TestGetFormatter:
    def _fmt(self, value: float, **kwargs: Any) -> str:
        """Helper that builds the formatter with ``**kwargs`` and evaluates it
        at ``value``."""
        base_fmt = kwargs.pop("base_fmt", "#.3g")
        f = Wall._get_formatter(base_fmt, **kwargs)
        # ticker.FuncFormatter is callable (value, pos)
        return f(value, 0)

    # --- basic numeric ranges -----------------------------------------------

    def test_small_value_uses_base_fmt(self) -> None:
        assert self._fmt(1.88, base_fmt=".2f", prefix="$") == "$1.88"

    def test_four_figure_uses_commas_not_K(self) -> None:
        assert self._fmt(2700, base_fmt="#.3g", prefix="$") == "$2,700"

    def test_edge_of_thousands_range(self) -> None:
        assert self._fmt(999, base_fmt="#.3g", prefix="$") == "$999"

    def test_k_suffix_at_10k_threshold(self) -> None:
        # 10,000 → divide by 1000 → 10 → "#.3g" → "10.0" → rstripped "." → "10.0K"
        assert self._fmt(10_000, base_fmt="#.3g", prefix="$") == "$10.0K"

    def test_m_suffix(self) -> None:
        # 1,200,000 → 1.2 → "#.3g" → "1.20" → rstrip → "1.20"
        assert self._fmt(1_200_000, base_fmt="#.3g", prefix="$") == "$1.20M"

    def test_b_suffix(self) -> None:
        assert self._fmt(5_000_000_000, base_fmt="#.3g", prefix="$") == "$5.00B"

    def test_huge_value_beyond_b_uses_commas_with_suffix(self) -> None:
        # 5 trillion → B branch divides by 1B → 5000 → ",.0f" → "5,000B"
        assert self._fmt(5e12, base_fmt="#.3g", prefix="$") == "$5,000B"

    def test_zero_small_base_fmt(self) -> None:
        assert self._fmt(0.0, base_fmt=".2f", prefix="$") == "$0.00"

    # --- scale & offset -----------------------------------------------------

    def test_scale_converts_units(self) -> None:
        # Raw x=1.88 USD, scale ETH/USD ≈ 4.33e-4 → value ≈ 0.000815 ETH
        result = self._fmt(1.88, base_fmt=".5f", scale=4.33e-4, prefix="Ξ ")
        assert result == "Ξ 0.00081"

    def test_offset_creates_percent_display(self) -> None:
        """Percent-from-peg: scale = 100/primary, offset = -100."""
        primary = 1.162
        # x = primary → 0%
        assert (
            self._fmt(
                primary, base_fmt="+.3g", scale=100 / primary, offset=-100, suffix="%"
            )
            == "+0%"
        )
        # x = 1.01 * primary → +1%
        assert (
            self._fmt(
                primary * 1.01,
                base_fmt="+.3g",
                scale=100 / primary,
                offset=-100,
                suffix="%",
            )
            == "+1%"
        )
        # x = 0.99 * primary → -1%
        assert (
            self._fmt(
                primary * 0.99,
                base_fmt="+.3g",
                scale=100 / primary,
                offset=-100,
                suffix="%",
            )
            == "-1%"
        )

    def test_suffix_appended_after_modifier(self) -> None:
        # 10k with "K" modifier + "%" suffix → "10.0K%" (contrived, but checks
        # that modifier and suffix both land)
        assert self._fmt(10_000, base_fmt="#.3g", suffix="%") == "10.0K%"


# --- Label aggregation for stackplot ---------------------------------------


class _FakeExchange:
    """Minimal Exchange stand-in with str() + color for label aggregation."""

    def __init__(self, name: str, color: str):
        self._name = name
        self._color = color

    def __str__(self) -> str:
        return self._name

    @property
    def color(self) -> str:
        return self._color


class TestLabelExchangeData:
    def _depth(self, value: float, n: int = 5) -> np.ndarray:
        return np.full(n, value, dtype=float)

    def test_below_max_unique_no_other_bucket(self) -> None:
        data = {
            _FakeExchange("A", "#111"): self._depth(10),
            _FakeExchange("B", "#222"): self._depth(5),
        }
        # Dict insertion order is preserved in Python 3.7+, so OrderedDict just
        # reflects iteration order here.
        from collections import OrderedDict

        result = Wall._label_exchange_data(  # type: ignore[type-var]
            OrderedDict(data), max_unique=3, color_other="#999"
        )
        assert len(result) == 2
        assert [row[1] for row in result] == ["A", "B"]
        assert [row[2] for row in result] == ["#111", "#222"]

    def test_overflow_aggregates_into_other(self) -> None:
        from collections import OrderedDict

        data = OrderedDict(
            {
                _FakeExchange("A", "#111"): self._depth(10),
                _FakeExchange("B", "#222"): self._depth(5),
                _FakeExchange("C", "#333"): self._depth(2),
                _FakeExchange("D", "#444"): self._depth(1),
            }
        )
        result = Wall._label_exchange_data(data, max_unique=2, color_other="#999")  # type: ignore[type-var]
        assert len(result) == 3
        assert [row[1] for row in result] == ["A", "B", "Other"]
        assert result[2][2] == "#999"
        # "Other" bucket sums the trailing entries.
        np.testing.assert_array_equal(result[2][0], self._depth(2) + self._depth(1))

    def test_empty_data_returns_empty(self) -> None:
        from collections import OrderedDict

        result = Wall._label_exchange_data(
            OrderedDict(), max_unique=3, color_other="#999"
        )
        assert result == []


# --- Fakes for depth / fetch / plot tests ----------------------------------


class _FakeLiquidity:
    """Minimal Liquidity: only `.price` and `.depth_at(x)` are used."""

    def __init__(self, price: float, depth: float = 5.0) -> None:
        self.price = price
        self._depth = depth

    def depth_at(self, _x: float) -> float:
        return self._depth


class _FakeExchangeWithMarkets:
    """Stands in for both CEX (`get_liquidity(session)`) and DEX
    (`get_liquidity()`); `*args` absorbs the optional session."""

    def __init__(
        self,
        name: str,
        markets: dict[Any, _FakeLiquidity] | BaseException,
        color: str = "#111",
    ) -> None:
        self._name = name
        self._markets = markets
        self.color = color

    def __str__(self) -> str:
        return self._name

    async def get_liquidity(self, *_args: Any) -> dict[Any, _FakeLiquidity]:
        if isinstance(self._markets, BaseException):
            raise self._markets
        return self._markets


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False


def _make_cog(bot: Any) -> Wall:
    # Sidestep GroupCog.__init__ + the real CEX/DEX construction; the methods
    # under test only need `bot` and the cached-DEX slots.
    cog = Wall.__new__(Wall)
    cog.bot = bot
    cog.dex_rpl = None
    cog.dex_reth = None
    return cog


def _config() -> MarketConfig:
    return MarketConfig(
        title="Test Depth",
        primary_prefix="$",
        secondary_prefix="Ξ ",
        step_size=1.0,
        default_min_multiplier=0.0,
        default_max_multiplier=5.0,
    )


# --- _get_market_depth_and_liquidity ---------------------------------------


class TestMarketDepthAndLiquidity:
    def test_different_units_back_converts_via_spot(self) -> None:
        x = np.array([8.0, 10.0, 12.0])
        markets: dict[str, Any] = {"m": _FakeLiquidity(price=10.0, depth=5.0)}
        depth, liquidity = Wall._get_market_depth_and_liquidity(markets, x, 10.0)
        # conv = price/ref = 1 → depth is the raw depth_at everywhere; the
        # endpoint liquidity is depth_at(x[0]) + depth_at(x[-1]).
        np.testing.assert_array_equal(depth, np.array([5.0, 5.0, 5.0]))
        assert liquidity == 10.0

    def test_same_units_aligns_pool_to_chart_center(self) -> None:
        x = np.array([8.0, 10.0, 12.0])
        markets: dict[str, Any] = {"m": _FakeLiquidity(price=10.0, depth=5.0)}
        # ref (12) differs from pool spot (10): points on the far side of the
        # spot get raw+offset, the near side gets |raw-offset| (= 0 here).
        depth, liquidity = Wall._get_market_depth_and_liquidity(
            markets, x, 12.0, same_units=True
        )
        np.testing.assert_array_equal(depth, np.array([10.0, 0.0, 0.0]))
        assert liquidity == 10.0


# --- _get_cex_data / _get_dex_data -----------------------------------------


class TestGetCexData:
    async def test_sorts_by_liquidity_and_reports_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakeSession())
        x = np.array([10.0, 10.0])
        big = _FakeExchangeWithMarkets("Big", {"m": _FakeLiquidity(10.0, depth=9.0)})
        small = _FakeExchangeWithMarkets(
            "Small", {"m": _FakeLiquidity(10.0, depth=1.0)}
        )
        boom = _FakeExchangeWithMarkets("Boom", RuntimeError("down"))

        bot = make_bot()
        cog = _make_cog(bot)
        result = await cog._get_cex_data({big, small, boom}, x, 10.0)  # type: ignore[arg-type]

        assert [str(e) for e in result] == ["Big", "Small"]
        bot.report_error.assert_awaited()


class TestGetDexData:
    async def test_skips_pools_without_liquidity(self) -> None:
        x = np.array([10.0, 10.0])
        has = _FakeExchangeWithMarkets("Has", {"m": _FakeLiquidity(10.0, depth=4.0)})
        empty = _FakeExchangeWithMarkets("Empty", {})

        cog = _make_cog(make_bot())
        result = await cog._get_dex_data({has, empty}, x, 10.0)  # type: ignore[arg-type]

        assert [str(e) for e in result] == ["Has"]


# --- _plot_data ------------------------------------------------------------


def _exchange_series(name: str, color: str, n: int = 5) -> tuple[Any, np.ndarray]:
    return _FakeExchangeWithMarkets(name, {}, color), np.full(n, 3.0)


class TestPlotData:
    def _run_plot(
        self, cex: OrderedDict[Any, np.ndarray], dex: OrderedDict[Any, np.ndarray]
    ) -> figure.Figure:
        x = np.linspace(1.0, 2.0, 5)
        fmt = Wall._get_formatter(".2f", prefix="$")
        return Wall._plot_data(x, 1.5, cex, dex, _config(), fmt, fmt, fmt)

    def test_renders_with_both_dex_and_cex(self) -> None:
        e1, y1 = _exchange_series("CexA", "#111")
        e2, y2 = _exchange_series("DexA", "#222")
        fig = self._run_plot(OrderedDict({e1: y1}), OrderedDict({e2: y2}))
        assert isinstance(fig, figure.Figure)
        plt.close(fig)

    def test_renders_cex_only(self) -> None:
        e1, y1 = _exchange_series("CexA", "#111")
        fig = self._run_plot(OrderedDict({e1: y1}), OrderedDict())
        assert isinstance(fig, figure.Figure)
        plt.close(fig)

    def test_renders_dex_only(self) -> None:
        e2, y2 = _exchange_series("DexA", "#222")
        fig = self._run_plot(OrderedDict(), OrderedDict({e2: y2}))
        assert isinstance(fig, figure.Figure)
        plt.close(fig)


# --- _run ------------------------------------------------------------------


def _const_fetch(val: float) -> Any:
    async def fetch(
        _set: Any, x: np.ndarray, _ref: float, *, same_units: bool = False
    ) -> OrderedDict[Any, np.ndarray]:
        return OrderedDict(
            {_FakeExchangeWithMarkets(f"E{val}", {}): np.full(len(x), val)}
        )

    return fetch


async def _empty_fetch(
    _set: Any, x: np.ndarray, _ref: float, *, same_units: bool = False
) -> OrderedDict[Any, np.ndarray]:
    return OrderedDict()


async def _call_run(
    cog: Wall,
    interaction: Any,
    *,
    min_price: float | None = None,
    max_price: float | None = None,
    sources: str = "All",
) -> None:
    fmt = Wall._get_formatter(".2f", prefix="$")
    await cog._run(
        interaction,
        min_price,
        max_price,
        sources,  # type: ignore[arg-type]
        config=_config(),
        primary_price=10.0,
        secondary_price=5.0,
        bottom_formatter=fmt,
        top_formatter=fmt,
        y_right_formatter=fmt,
        cex_set={"a"},  # type: ignore[arg-type]
        dex_set={"b"},  # type: ignore[arg-type]
    )


class TestRun:
    async def test_sends_image_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cog = _make_cog(make_bot())
        monkeypatch.setattr(cog, "_get_cex_data", _const_fetch(3.0))
        monkeypatch.setattr(cog, "_get_dex_data", _const_fetch(2.0))

        interaction = make_interaction()
        await _call_run(cog, interaction)
        assert interaction.followup.send.call_args.kwargs.get("files")

    async def test_negative_prices_are_relative_to_spot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Negative bounds are interpreted relative to the current price; this
        # only needs to not blow up and still render.
        cog = _make_cog(make_bot())
        monkeypatch.setattr(cog, "_get_cex_data", _const_fetch(3.0))
        monkeypatch.setattr(cog, "_get_dex_data", _const_fetch(2.0))

        interaction = make_interaction()
        await _call_run(cog, interaction, min_price=-2.0, max_price=-2.0)
        assert interaction.followup.send.call_args.kwargs.get("files")

    async def test_dex_only_skips_cex_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cog = _make_cog(make_bot())
        monkeypatch.setattr(cog, "_get_dex_data", _const_fetch(2.0))
        # If CEX were fetched despite sources="DEX", this would raise.
        monkeypatch.setattr(
            cog, "_get_cex_data", AsyncMock(side_effect=AssertionError("cex fetched"))
        )

        interaction = make_interaction()
        await _call_run(cog, interaction, sources="DEX")
        assert interaction.followup.send.call_args.kwargs.get("files")

    async def test_cex_only_skips_dex_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cog = _make_cog(make_bot())
        monkeypatch.setattr(cog, "_get_cex_data", _const_fetch(3.0))
        monkeypatch.setattr(
            cog, "_get_dex_data", AsyncMock(side_effect=AssertionError("dex fetched"))
        )

        interaction = make_interaction()
        await _call_run(cog, interaction, sources="CEX")
        assert interaction.followup.send.call_args.kwargs.get("files")

    async def test_fetch_error_reports_and_sends_gif(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bot = make_bot()
        cog = _make_cog(bot)
        monkeypatch.setattr(
            cog, "_get_dex_data", AsyncMock(side_effect=RuntimeError("boom"))
        )

        interaction = make_interaction()
        await _call_run(cog, interaction)
        bot.report_error.assert_awaited()
        call = interaction.followup.send.call_args
        assert "giphy" in call.kwargs["embed"].image.url

    async def test_no_data_sends_failure_gif(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cog = _make_cog(make_bot())
        monkeypatch.setattr(cog, "_get_cex_data", _empty_fetch)
        monkeypatch.setattr(cog, "_get_dex_data", _empty_fetch)

        interaction = make_interaction()
        await _call_run(cog, interaction)
        call = interaction.followup.send.call_args
        assert "files" not in call.kwargs
        assert "giphy" in call.kwargs["embed"].image.url


class TestInit:
    def test_populates_cex_set_and_lazy_dex_slots(self) -> None:
        cog = Wall(make_bot())
        assert len(cog.cex_rpl) > 0
        assert cog.dex_rpl is None
        assert cog.dex_reth is None
