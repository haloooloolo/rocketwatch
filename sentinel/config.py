import tomllib

from pydantic import BaseModel, Field, model_validator


class DiscordConfig(BaseModel):
    token: str


class KeyDefaults(BaseModel):
    delete_message_max_age: int = 900
    lock_thread_max_age: int = 3600
    delete_thread_max_age: int = 3600
    timeout_member_max_duration: int = 86400
    kick_member_max_age: int = 0
    ban_member_max_age: int = 0
    max_actions_per_hour: int = 100


class KeyConfig(KeyDefaults):
    secret: str
    allowed_server_ids: list[int] = []


class ApiConfig(BaseModel):
    defaults: KeyDefaults = Field(default_factory=KeyDefaults)
    keys: list[KeyConfig]
    host: str = "0.0.0.0"
    port: int = 8080

    @model_validator(mode="before")
    @classmethod
    def apply_defaults(cls, data: dict) -> dict:
        defaults = data.get("defaults", {})
        data["keys"] = [{**defaults, **key} for key in data.get("keys", [])]
        return data


class Config(BaseModel):
    discord: DiscordConfig
    api: ApiConfig


class _ConfigProxy:
    _instance: Config | None = None

    def __init__(self, path: str = "config.toml") -> None:
        self.__path = path

    def __load_config(self) -> None:
        with open(self.__path, "rb") as f:
            data = tomllib.load(f)
        cfg._instance = Config(**data)

    def __getattr__(self, name: str):
        if self._instance is None:
            self.__load_config()
        return getattr(self._instance, name)


cfg = _ConfigProxy()
