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
)


def make_cfg(chain: str = "mainnet") -> Config:
    """Build a minimal Config with the given `rocketpool.chain` value."""
    return Config(
        discord=DiscordConfig(
            secret="test",
            owner=DiscordOwner(user_id=1, server_id=2),
            channels={"default": 1, "errors": 1, "report_scams": 1},
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
            manual_addresses={"rocketStorage": "0x" + "0" * 40},
            support=RocketPoolSupport(server_id=1, channel_id=1, moderator_id=1),
            dm_warning=DmWarningConfig(channels=[]),
        ),
        events=EventsConfig(lookback_distance=10, genesis=0, block_batch_size=10),
    )
