import tomllib

from pydantic import BaseModel


class DiscordConfig(BaseModel):
    token: str


class KeyConfig(BaseModel):
    secret: str
    allowed_server_ids: list[int] = []
    delete_message_max_age_seconds: int = 900
    lock_thread_max_age_seconds: int = 3600
    delete_thread_max_age_seconds: int = 3600
    timeout_member_max_duration_seconds: int = 86400
    kick_max_member_age_seconds: int = 604800
    ban_max_member_age_seconds: int = 604800
    max_actions_per_hour: int = 100


class ApiConfig(BaseModel):
    keys: list[KeyConfig]
    host: str = "0.0.0.0"
    port: int = 8080


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
