"""Deterministic stand-ins for explorer links and block timestamps in embeds."""

from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock

import pytest

from rocketwatch.utils import embeds, type_markers

BLOCK_TS = 1_700_000_000


async def _link(
    target: str,
    name: str = "",
    prefix: str | None = "",
    name_fmt: Any = None,
    block: Any = "latest",
) -> str:
    return f"{prefix or ''}[{name or target}](explorer/{target})"


def stub_explorer_links(
    monkeypatch: pytest.MonkeyPatch, *importers: ModuleType
) -> None:
    """Stub link/timestamp lookups in the embed helpers and in *importers*,
    modules that imported ``el_explorer_url`` by name."""
    for module in (embeds, type_markers, *importers):
        monkeypatch.setattr(module, "el_explorer_url", _link)
    monkeypatch.setattr(
        type_markers, "get_sea_creature_for_address", AsyncMock(return_value="")
    )
    monkeypatch.setattr(embeds, "block_to_ts", AsyncMock(return_value=BLOCK_TS))
