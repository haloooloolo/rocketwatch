import tomllib
from pathlib import Path

import pytest

from utils.config import (
    Config,
    ConsensusLayerConfig,
    DiscordConfig,
    DiscordOwner,
    DmWarningConfig,
    EventsConfig,
    ExecutionLayerConfig,
    ExecutionLayerEndpoint,
    ModulesConfig,
    MongoDBConfig,
    OtherConfig,
    RocketPoolConfig,
    RocketPoolSupport,
    SecretsConfig,
    StatusMessageConfig,
)


def _minimal_config(**overrides) -> Config:
    defaults = {
        "discord": DiscordConfig(
            secret="test-secret",
            owner=DiscordOwner(user_id=1, server_id=2),
            channels={"default": 100},
        ),
        "execution_layer": ExecutionLayerConfig(
            explorer="https://etherscan.io",
            endpoint=ExecutionLayerEndpoint(current="http://localhost:8545", mainnet="http://localhost:8545"),
            etherscan_secret="test",
        ),
        "consensus_layer": ConsensusLayerConfig(
            explorer="https://beaconcha.in",
            endpoint="http://localhost:5052",
            beaconcha_secret="test",
        ),
        "mongodb": MongoDBConfig(uri="mongodb://localhost:27017"),
        "rocketpool": RocketPoolConfig(
            manual_addresses={"rocketStorage": "0x1234"},
            dao_multisigs=["0xabcd"],
            support=RocketPoolSupport(user_ids=[1], role_ids=[2], server_id=3, channel_id=4, moderator_id=5),
            dm_warning=DmWarningConfig(channels=[100]),
        ),
        "events": EventsConfig(lookback_distance=100, genesis=0, block_batch_size=50),
    }
    defaults.update(overrides)
    return Config(**defaults)


class TestConfigConstruction:
    def test_minimal_config(self):
        cfg = _minimal_config()
        assert cfg.discord.secret == "test-secret"
        assert cfg.log_level == "DEBUG"

    def test_defaults(self):
        cfg = _minimal_config()
        assert cfg.modules == ModulesConfig()
        assert cfg.modules.include == []
        assert cfg.modules.exclude == []
        assert cfg.modules.enable_commands is True
        assert cfg.other == OtherConfig()
        assert cfg.other.secrets.wakatime == ""
        assert cfg.rocketpool.chain == "mainnet"

    def test_override_defaults(self):
        cfg = _minimal_config(log_level="INFO")
        assert cfg.log_level == "INFO"

    def test_archive_endpoint_optional(self):
        cfg = _minimal_config()
        assert cfg.execution_layer.endpoint.archive is None

    def test_archive_endpoint_set(self):
        cfg = _minimal_config(
            execution_layer=ExecutionLayerConfig(
                explorer="https://etherscan.io",
                endpoint=ExecutionLayerEndpoint(
                    current="http://localhost:8545",
                    mainnet="http://localhost:8545",
                    archive="http://localhost:8546",
                ),
                etherscan_secret="test",
            )
        )
        assert cfg.execution_layer.endpoint.archive == "http://localhost:8546"


class TestConfigValidation:
    def test_missing_required_field(self):
        with pytest.raises(Exception):
            Config(discord=DiscordConfig(
                secret="test",
                owner=DiscordOwner(user_id=1, server_id=2),
                channels={},
            ))

    def test_wrong_type_user_id(self):
        with pytest.raises(Exception):
            DiscordOwner(user_id="not_an_int", server_id=2)

    def test_int_coercion(self):
        owner = DiscordOwner(user_id="123", server_id="456")
        assert owner.user_id == 123
        assert owner.server_id == 456


class TestStatusMessageConfig:
    def test_basic(self):
        smc = StatusMessageConfig(plugin="test_plugin", cooldown=60)
        assert smc.plugin == "test_plugin"
        assert smc.cooldown == 60
        assert smc.fields == []

    def test_with_fields(self):
        smc = StatusMessageConfig(
            plugin="test_plugin",
            cooldown=30,
            fields=[{"name": "field1", "value": "val1"}],
        )
        assert len(smc.fields) == 1


class TestSecretsConfig:
    def test_all_default_empty(self):
        s = SecretsConfig()
        assert s.wakatime == ""
        assert s.cronitor == ""
        assert s.anthropic == ""

    def test_partial_override(self):
        s = SecretsConfig(wakatime="my-key")
        assert s.wakatime == "my-key"
        assert s.cronitor == ""


class TestSampleConfig:
    def test_sample_config_validates(self):
        sample_path = Path(__file__).resolve().parent.parent / "rocketwatch" / "config.toml.sample"
        with open(sample_path, "rb") as f:
            data = tomllib.load(f)
        cfg = Config(**data)
        assert cfg.log_level == "INFO"
        assert cfg.rocketpool.chain
