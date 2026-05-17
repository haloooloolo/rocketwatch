import pytest
from discord import Color

from rocketwatch.utils.config import (
    Config,
    ConsensusLayerConfig,
    DiscordConfig,
    DiscordOwner,
    DmWarningConfig,
    EventsConfig,
    ExecutionLayerConfig,
    ExecutionLayerEndpoint,
    MongoDBConfig,
    RocketPoolConfig,
    RocketPoolSupport,
    cfg,
)
from rocketwatch.utils.embeds import CustomColors, Embed, format_value


def _make_cfg(chain: str = "mainnet") -> Config:
    return Config(
        discord=DiscordConfig(
            secret="test",
            owner=DiscordOwner(user_id=1, server_id=2),
            channels={"default": 1},
        ),
        execution_layer=ExecutionLayerConfig(
            explorer="https://etherscan.io",
            endpoint=ExecutionLayerEndpoint(current=["http://localhost"]),
        ),
        consensus_layer=ConsensusLayerConfig(
            explorer="https://beaconcha.in",
            endpoint=["http://localhost"],
        ),
        mongodb=MongoDBConfig(uri="mongodb://localhost"),
        rocketpool=RocketPoolConfig(
            chain=chain,
            manual_addresses={"rocketStorage": "0x0"},
            dao_multisigs=[],
            support=RocketPoolSupport(server_id=1, channel_id=1, moderator_id=1),
            dm_warning=DmWarningConfig(channels=[]),
        ),
        events=EventsConfig(lookback_distance=10, genesis=0, block_batch_size=10),
    )


@pytest.fixture
def mainnet_cfg(monkeypatch):
    monkeypatch.setattr(cfg, "_instance", _make_cfg("mainnet"))
    yield
    monkeypatch.setattr(cfg, "_instance", None)


@pytest.fixture
def testnet_cfg(monkeypatch):
    monkeypatch.setattr(cfg, "_instance", _make_cfg("holesky"))
    yield
    monkeypatch.setattr(cfg, "_instance", None)


class TestFormatValue:
    def test_zero(self):
        # Zero is special-cased (log10 would blow up); should return "0".
        assert format_value(0) == "0"

    def test_small_integer(self):
        assert format_value(5) == "5"

    def test_thousands_get_commas(self):
        assert format_value(1234567) == "1,234,567"

    def test_small_floats_keep_meaningful_precision(self):
        # A tiny value should not be rounded to "0".
        out = format_value(0.00012345678)
        assert out.startswith("0.0001")
        assert out != "0"

    def test_trailing_integer_drops_decimal(self):
        # Whole-number floats should display without a trailing ".0".
        assert format_value(5.0) == "5"

    def test_negative_values_format_with_sign(self):
        # Negative values must be supported and round-trip the sign.
        out = format_value(-1234)
        assert out.startswith("-")
        assert "1,234" in out

    def test_large_floats_drop_excess_decimals(self):
        # For values whose integer part already has 6 digits, sub-integer noise
        # should not appear in the output — there's no useful precision left.
        out = format_value(123456.789)
        assert "123,456" in out or "123,457" in out
        assert "." not in out


class TestCustomColors:
    def test_colors_are_discord_color_instances(self):
        assert isinstance(CustomColors.RED, Color)
        assert isinstance(CustomColors.ORANGE, Color)
        assert isinstance(CustomColors.YELLOW, Color)
        assert isinstance(CustomColors.GREEN, Color)


class TestEmbedFooter:
    def test_default_color_is_orange(self, mainnet_cfg):
        e = Embed()
        assert e.color == CustomColors.ORANGE

    def test_explicit_color_overrides_default(self, mainnet_cfg):
        e = Embed(color=CustomColors.RED)
        assert e.color == CustomColors.RED

    def test_mainnet_footer_omits_chain(self, mainnet_cfg):
        e = Embed()
        assert e.footer.text is not None
        assert "Chain:" not in e.footer.text
        assert "Created by" in e.footer.text

    def test_testnet_footer_includes_chain(self, testnet_cfg):
        e = Embed()
        assert e.footer.text is not None
        assert "Chain: Holesky" in e.footer.text

    def test_set_footer_parts_appends_to_base(self, mainnet_cfg):
        e = Embed()
        e.set_footer_parts(["Block 123", "Synced"])
        assert e.footer.text is not None
        assert "Block 123" in e.footer.text
        assert "Synced" in e.footer.text
        # The base "Created by ..." prefix is preserved.
        assert e.footer.text.startswith("Created by")

    def test_set_footer_parts_replaces_previous(self, mainnet_cfg):
        e = Embed()
        e.set_footer_parts(["A"])
        e.set_footer_parts(["B"])
        assert e.footer.text is not None
        assert "A" not in e.footer.text
        assert "B" in e.footer.text
