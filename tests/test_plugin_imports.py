"""Smoke test: every plugin module under rocketwatch/plugins/ must import cleanly.

This is the cheapest possible safety net for the 40+ plugins with no real coverage —
it catches syntax errors, broken imports, and renamed symbols at module-import time.
It does NOT exercise any plugin behaviour; it just guarantees the bot would not crash
at plugin-load time on a fresh deploy.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# Plugins live at rocketwatch/plugins/<name>/<name>.py — discover them dynamically
# so newly-added plugins are smoke-tested automatically.
_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "rocketwatch" / "plugins"


def _discover_plugins() -> list[str]:
    names = []
    for p in sorted(_PLUGINS_DIR.iterdir()):
        if p.is_dir() and (p / f"{p.name}.py").exists():
            names.append(p.name)
    return names


@pytest.mark.parametrize("plugin_name", _discover_plugins())
def test_plugin_imports(plugin_name: str) -> None:
    """Importing the plugin's top-level module must not raise."""
    importlib.import_module(f"rocketwatch.plugins.{plugin_name}.{plugin_name}")


def test_at_least_one_plugin_discovered() -> None:
    # Guard against the discovery glob silently breaking and the parametrize collapsing to zero cases.
    assert len(_discover_plugins()) >= 10
