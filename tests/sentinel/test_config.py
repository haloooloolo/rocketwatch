import pytest
from config import ApiConfig, KeyConfig, KeyDefaults
from pydantic import ValidationError


class TestKeyDefaults:
    def test_default_values(self):
        d = KeyDefaults()
        assert d.delete_message_max_age is None
        assert d.lock_thread_max_age is None
        assert d.delete_thread_max_age is None
        assert d.timeout_member_max_duration is None
        assert d.kick_member_max_age is None
        assert d.ban_member_max_age is None
        assert d.max_actions_per_hour is None


class TestKeyConfig:
    def test_requires_secret(self):
        with pytest.raises(ValidationError):
            KeyConfig()

    def test_inherits_defaults(self):
        key = KeyConfig(secret="s")
        assert key.delete_message_max_age is None
        assert key.max_actions_per_hour is None

    def test_overrides_defaults(self):
        key = KeyConfig(secret="s", delete_message_max_age=60)
        assert key.delete_message_max_age == 60
        assert key.lock_thread_max_age is None

    def test_allowed_server_ids_default_empty(self):
        key = KeyConfig(secret="s")
        assert key.allowed_server_ids == []


class TestApiConfig:
    def test_apply_defaults_merges(self):
        api = ApiConfig(
            defaults={"max_actions_per_hour": 50},
            keys=[{"secret": "s"}],
        )
        assert api.keys[0].max_actions_per_hour == 50

    def test_key_overrides_defaults(self):
        api = ApiConfig(
            defaults={"max_actions_per_hour": 50},
            keys=[{"secret": "s", "max_actions_per_hour": 200}],
        )
        assert api.keys[0].max_actions_per_hour == 200

    def test_no_defaults_section(self):
        api = ApiConfig(keys=[{"secret": "s"}])
        assert api.keys[0].max_actions_per_hour is None

    def test_multiple_keys(self):
        api = ApiConfig(
            defaults={"max_actions_per_hour": 42},
            keys=[{"secret": "a"}, {"secret": "b"}],
        )
        assert all(k.max_actions_per_hour == 42 for k in api.keys)
        assert api.keys[0].secret == "a"
        assert api.keys[1].secret == "b"
