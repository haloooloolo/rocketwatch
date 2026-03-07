import base64
import zlib

from utils.readable import (
    decode_abi,
    prettify_json_string,
    pretty_time,
    render_tree_legacy,
    s_hex,
)


class TestUptime:
    def test_zero_seconds(self):
        assert pretty_time(0) == "0 seconds"

    def test_seconds_only(self):
        assert pretty_time(45) == "45 seconds"

    def test_one_minute(self):
        assert pretty_time(60) == "1 minute"

    def test_minutes_and_seconds(self):
        assert pretty_time(90) == "1 minute 30 seconds"

    def test_one_hour(self):
        assert pretty_time(3600) == "1 hour"

    def test_hours_and_minutes(self):
        assert pretty_time(3660) == "1 hour 1 minute"

    def test_one_day(self):
        assert pretty_time(86400) == "1 day"

    def test_plural_days(self):
        assert pretty_time(2 * 86400) == "2 days"

    def test_days_and_hours(self):
        t = 86400 + 7200 + 180 + 4
        assert pretty_time(t) == "1 day 2 hours"


class TestPrettifyJsonString:
    def test_basic(self):
        result = prettify_json_string('{"a":1,"b":2}')
        assert '"a": 1' in result
        assert '"b": 2' in result
        assert "\n" in result


class TestDecodeAbi:
    def test_roundtrip(self):
        original = '[{"type":"function","name":"test"}]'
        compressed = base64.b64encode(zlib.compress(original.encode("ascii"), wbits=15))
        assert decode_abi(compressed) == original


class TestSHex:
    def test_truncates_to_10(self):
        assert s_hex("0x1234567890abcdef") == "0x12345678"

    def test_short_string(self):
        assert s_hex("0x12") == "0x12"


class TestRenderTreeLegacy:
    def test_flat_tree(self):
        data = {"active": 10, "inactive": 5}
        result = render_tree_legacy(data, "Minipools")
        assert "Minipools:" in result
        assert "15" in result  # total
        assert "10" in result
        assert "5" in result

    def test_nested_tree(self):
        data = {
            "staking": {"8 ETH": 100, "16 ETH": 50},
            "dissolved": 3,
        }
        result = render_tree_legacy(data, "Minipools")
        assert "Minipools:" in result
        assert "153" in result  # total

    def test_empty_branches_filtered(self):
        data = {"active": 10, "empty": 0}
        result = render_tree_legacy(data, "Test")
        assert "Empty" not in result
