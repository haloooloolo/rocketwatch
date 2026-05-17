"""Smoke test: every plugin module under rocketwatch/plugins/ must import cleanly.

This is the cheapest possible safety net for the 40+ plugins with no real coverage —
it catches syntax errors, broken imports, and renamed symbols at module-import time.
It does NOT exercise any plugin behaviour; it just guarantees the bot would not crash
at plugin-load time on a fresh deploy.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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

# Plugins live at rocketwatch/plugins/<name>/<name>.py — discover them dynamically
# so newly-added plugins are smoke-tested automatically.
_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "rocketwatch" / "plugins"


def _discover_plugins() -> list[str]:
    names = []
    for p in sorted(_PLUGINS_DIR.iterdir()):
        if p.is_dir() and (p / f"{p.name}.py").exists():
            names.append(p.name)
    return names


def _ensure_cfg() -> None:
    """Materialise a minimal config so plugins that read cfg at import time succeed."""
    if cfg._instance is not None:
        return
    cfg._instance = Config(
        discord=DiscordConfig(
            secret="test",
            owner=DiscordOwner(user_id=1, server_id=2),
            channels={"errors": 1, "default": 1},
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
            chain="mainnet",
            manual_addresses={"rocketStorage": "0x" + "0" * 40},
            dao_multisigs=[],
            support=RocketPoolSupport(server_id=1, channel_id=1, moderator_id=1),
            dm_warning=DmWarningConfig(channels=[]),
        ),
        events=EventsConfig(lookback_distance=10, genesis=0, block_batch_size=10),
    )


def _install_extra_stubs() -> None:
    """Extend the conftest stubs with the helpers/modules plugins import at top level."""
    # The conftest stubs visibility.is_hidden but not is_hidden_role_controlled.
    vis = sys.modules.get("rocketwatch.utils.visibility")
    if vis is not None and not hasattr(vis, "is_hidden_role_controlled"):
        vis.is_hidden_role_controlled = MagicMock(return_value=False)


@pytest.fixture(scope="module", autouse=True)
def _import_env():
    _ensure_cfg()
    _install_extra_stubs()
    yield


@pytest.mark.parametrize("plugin_name", _discover_plugins())
def test_plugin_imports(plugin_name: str) -> None:
    """Importing the plugin's top-level module must not raise."""
    importlib.import_module(f"rocketwatch.plugins.{plugin_name}.{plugin_name}")


def test_at_least_one_plugin_discovered() -> None:
    # Guard against the discovery glob silently breaking and the parametrize collapsing to zero cases.
    assert len(_discover_plugins()) >= 10
