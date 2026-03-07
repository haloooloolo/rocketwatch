import tomllib
from pydantic import BaseModel


class DiscordOwner(BaseModel):
    user_id: int
    server_id: int


class DiscordConfig(BaseModel):
    secret: str
    owner: DiscordOwner
    guilds: list[int]
    channels: dict[str, int]


class ExecutionLayerEndpoint(BaseModel):
    current: str
    mainnet: str
    archive: str | None = None


class ExecutionLayerConfig(BaseModel):
    explorer: str
    endpoint: ExecutionLayerEndpoint
    etherscan_secret: str


class ConsensusLayerConfig(BaseModel):
    explorer: str
    endpoint: str
    beaconcha_secret: str


class MongoDBConfig(BaseModel):
    uri: str


class RocketPoolSupport(BaseModel):
    user_ids: list[int]
    role_ids: list[int]
    server_id: int
    channel_id: int
    moderator_id: int


class DmWarningConfig(BaseModel):
    channels: list[int]


class RocketPoolConfig(BaseModel):
    chain: str = "mainnet"
    manual_addresses: dict[str, str]
    dao_multisigs: list[str]
    support: RocketPoolSupport
    dm_warning: DmWarningConfig


class ModulesConfig(BaseModel):
    include: list[str] = []
    exclude: list[str] = []
    enable_commands: bool = True


class StatusMessageConfig(BaseModel):
    plugin: str
    cooldown: int
    fields: list[dict[str, str]] = []


class EventsConfig(BaseModel):
    lookback_distance: int
    genesis: int
    block_batch_size: int
    status_message: dict[str, StatusMessageConfig] = {}


class SecretsConfig(BaseModel):
    wakatime: str = ""
    cronitor: str = ""
    anthropic: str = ""


class OtherConfig(BaseModel):
    mev_hashes: list[str] = []
    secrets: SecretsConfig = SecretsConfig()


class Config(BaseModel):
    log_level: str = "DEBUG"
    discord: DiscordConfig
    execution_layer: ExecutionLayerConfig
    consensus_layer: ConsensusLayerConfig
    mongodb: MongoDBConfig
    rocketpool: RocketPoolConfig
    modules: ModulesConfig = ModulesConfig()
    events: EventsConfig
    other: OtherConfig = OtherConfig()


def load_config(path: str = "config.toml") -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Config(**data)


cfg = load_config()
