from rocketwatch.plugins.apr.apr import (
    APRDatapoint,
    get_duration,
    get_period_change,
    to_apr,
)

YEAR_SECONDS = 365 * 24 * 60 * 60


def _dp(time: float, value: float, effectiveness: float = 1.0) -> APRDatapoint:
    return APRDatapoint(block=0, time=time, value=value, effectiveness=effectiveness)


class TestGetDuration:
    def test_positive_duration(self):
        assert get_duration(_dp(0, 1.0), _dp(100, 1.0)) == 100

    def test_zero_duration(self):
        assert get_duration(_dp(50, 1.0), _dp(50, 1.0)) == 0


class TestGetPeriodChange:
    def test_no_change(self):
        assert get_period_change(_dp(0, 1.0), _dp(1, 1.0)) == 0.0

    def test_10pct_increase_effective(self):
        change = get_period_change(_dp(0, 1.0), _dp(1, 1.1))
        assert abs(change - 0.1) < 1e-12

    def test_effectiveness_divides_when_virtual(self):
        # virtual (effective=False) divides by d2 effectiveness, giving the gross rate.
        d1 = _dp(0, 1.0, effectiveness=1.0)
        d2 = _dp(1, 1.1, effectiveness=0.5)
        gross = get_period_change(d1, d2, effective=False)
        assert abs(gross - 0.2) < 1e-12

    def test_decrease_returns_negative(self):
        assert get_period_change(_dp(0, 1.0), _dp(1, 0.9)) < 0


class TestToApr:
    def test_one_year_at_10_percent(self):
        d1 = _dp(0, 1.0)
        d2 = _dp(YEAR_SECONDS, 1.1)
        apr = to_apr(d1, d2)
        assert abs(apr - 0.1) < 1e-9

    def test_half_year_at_5_percent_annualizes_to_10(self):
        d1 = _dp(0, 1.0)
        d2 = _dp(YEAR_SECONDS // 2, 1.05)
        apr = to_apr(d1, d2)
        assert abs(apr - 0.1) < 1e-4

    def test_one_day_growth_extrapolates(self):
        # 0.027% in one day ≈ 9.9% APR
        day = 24 * 60 * 60
        d1 = _dp(0, 1.0)
        d2 = _dp(day, 1.000273)
        apr = to_apr(d1, d2)
        assert 0.09 < apr < 0.11

    def test_virtual_apr_higher_than_effective_when_effectiveness_under_one(self):
        d1 = _dp(0, 1.0, effectiveness=1.0)
        d2 = _dp(YEAR_SECONDS, 1.1, effectiveness=0.8)
        eff = to_apr(d1, d2, effective=True)
        virtual = to_apr(d1, d2, effective=False)
        assert virtual > eff
