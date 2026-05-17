"""Tests for pure helpers in plugins/wall/wall.py."""

import numpy as np

from rocketwatch.plugins.wall.wall import Wall

# --- Tick formatter ---------------------------------------------------------


class TestGetFormatter:
    def _fmt(self, value: float, **kwargs) -> str:
        """Helper that builds the formatter with ``**kwargs`` and evaluates it
        at ``value``."""
        base_fmt = kwargs.pop("base_fmt", "#.3g")
        f = Wall._get_formatter(base_fmt, **kwargs)
        # ticker.FuncFormatter is callable (value, pos)
        return f(value, 0)  # type: ignore[operator]

    # --- basic numeric ranges -----------------------------------------------

    def test_small_value_uses_base_fmt(self):
        assert self._fmt(1.88, base_fmt=".2f", prefix="$") == "$1.88"

    def test_four_figure_uses_commas_not_K(self):
        assert self._fmt(2700, base_fmt="#.3g", prefix="$") == "$2,700"

    def test_edge_of_thousands_range(self):
        assert self._fmt(999, base_fmt="#.3g", prefix="$") == "$999"

    def test_k_suffix_at_10k_threshold(self):
        # 10,000 → divide by 1000 → 10 → "#.3g" → "10.0" → rstripped "." → "10.0K"
        assert self._fmt(10_000, base_fmt="#.3g", prefix="$") == "$10.0K"

    def test_m_suffix(self):
        # 1,200,000 → 1.2 → "#.3g" → "1.20" → rstrip → "1.20"
        assert self._fmt(1_200_000, base_fmt="#.3g", prefix="$") == "$1.20M"

    def test_b_suffix(self):
        assert self._fmt(5_000_000_000, base_fmt="#.3g", prefix="$") == "$5.00B"

    def test_huge_value_beyond_b_uses_commas_with_suffix(self):
        # 5 trillion → B branch divides by 1B → 5000 → ",.0f" → "5,000B"
        assert self._fmt(5e12, base_fmt="#.3g", prefix="$") == "$5,000B"

    def test_zero_small_base_fmt(self):
        assert self._fmt(0.0, base_fmt=".2f", prefix="$") == "$0.00"

    # --- scale & offset -----------------------------------------------------

    def test_scale_converts_units(self):
        # Raw x=1.88 USD, scale ETH/USD ≈ 4.33e-4 → value ≈ 0.000815 ETH
        result = self._fmt(1.88, base_fmt=".5f", scale=4.33e-4, prefix="Ξ ")
        assert result == "Ξ 0.00081"

    def test_offset_creates_percent_display(self):
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

    def test_suffix_appended_after_modifier(self):
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

    def test_below_max_unique_no_other_bucket(self):
        data = {
            _FakeExchange("A", "#111"): self._depth(10),
            _FakeExchange("B", "#222"): self._depth(5),
        }
        # Dict insertion order is preserved in Python 3.7+, so OrderedDict just
        # reflects iteration order here.
        from collections import OrderedDict

        result = Wall._label_exchange_data(
            OrderedDict(data), max_unique=3, color_other="#999"
        )
        assert len(result) == 2
        assert [row[1] for row in result] == ["A", "B"]
        assert [row[2] for row in result] == ["#111", "#222"]

    def test_overflow_aggregates_into_other(self):
        from collections import OrderedDict

        data = OrderedDict(
            {
                _FakeExchange("A", "#111"): self._depth(10),
                _FakeExchange("B", "#222"): self._depth(5),
                _FakeExchange("C", "#333"): self._depth(2),
                _FakeExchange("D", "#444"): self._depth(1),
            }
        )
        result = Wall._label_exchange_data(data, max_unique=2, color_other="#999")
        assert len(result) == 3
        assert [row[1] for row in result] == ["A", "B", "Other"]
        assert result[2][2] == "#999"
        # "Other" bucket sums the trailing entries.
        np.testing.assert_array_equal(result[2][0], self._depth(2) + self._depth(1))

    def test_empty_data_returns_empty(self):
        from collections import OrderedDict

        result = Wall._label_exchange_data(
            OrderedDict(), max_unique=3, color_other="#999"
        )
        assert result == []
