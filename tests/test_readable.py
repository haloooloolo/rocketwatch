import base64
import zlib

from rocketwatch.utils.readable import (
    decode_abi,
    prettify_json_string,
    pretty_time,
    render_branch,
    render_tree,
    render_tree_legacy,
    s_hex,
)


class TestPrettyTime:
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

    def test_float_seconds(self):
        assert pretty_time(30.7) == "30 seconds"

    def test_float_minutes_and_seconds(self):
        assert pretty_time(90.3) == "1 minute 30 seconds"

    def test_float_hours(self):
        assert pretty_time(3600.9) == "1 hour"

    def test_float_days(self):
        assert pretty_time(86400.5) == "1 day"


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


class TestSHexEdge:
    def test_exactly_ten_chars(self):
        assert s_hex("0x12345678") == "0x12345678"


class TestRenderBranch:
    def test_leaf_produces_single_row_with_value(self):
        rows = render_branch("root", {"_value": 7}, "")
        assert len(rows) == 1
        # Each row is (label, value, depth); we care that the value reaches the output
        # at depth 0. The label format is implementation-detail.
        _, value, depth = rows[0]
        assert value == 7
        assert depth == 0

    def test_nested_one_level(self):
        data = {"_value": 5, "child": {"_value": 5}}
        rows = render_branch("root", data, "", reverse=True)
        # Parent first, then child (depths 0 and 1)
        depths = [r[2] for r in rows]
        values = [r[1] for r in rows]
        assert 0 in depths and 1 in depths
        assert 5 in values

    def test_underscore_keys_skipped(self):
        # Keys starting with "_" are metadata and must not appear as branches.
        data = {"_value": 3, "_hidden": {"_value": 99}, "shown": {"_value": 3}}
        rows = render_branch("root", data, "")
        rendered = "\n".join(r[0] for r in rows)
        assert "shown" in rendered
        assert "_hidden" not in rendered

    def test_max_depth_truncates(self):
        deep = {
            "_value": 1,
            "a": {"_value": 1, "b": {"_value": 1, "c": {"_value": 1}}},
        }
        rows = render_branch("root", deep, "", max_depth=1)
        rendered = "\n".join(r[0] for r in rows)
        assert "a" in rendered
        # Anything deeper than depth=1 must be pruned.
        assert "c" not in rendered


class TestRenderTree:
    def test_basic_render(self):
        data = {"active": {"_value": 10}, "exited": {"_value": 3}}
        out = render_tree(data, "Validators")
        assert "Validators" in out
        assert "active" in out
        assert "exited" in out

    def test_empty_states_filtered(self):
        # Top-level keys with falsy values are dropped before rendering.
        data = {
            "active": {"_value": 1},
            "empty": {},
        }
        out = render_tree(data, "Test")
        assert "active" in out
        assert "empty" not in out

    def test_uses_nbsp_in_output(self):
        # render_tree replaces spaces with U+00A0 so Discord won't collapse them.
        data = {"only": {"_value": 1}}
        out = render_tree(data, "X")
        assert "\u00a0" in out
        # No regular ASCII spaces should remain in the rendered output.
        assert " " not in out
