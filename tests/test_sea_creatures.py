from rocketwatch.utils.sea_creatures import (
    get_sea_creature_for_holdings,
    sea_creatures,
)


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
