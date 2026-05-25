from typing import Any

import aiohttp
import pytest
from discord import Embed

from rocketwatch.plugins.releases.releases import Releases
from tests.lib.discord_harness import make_bot, make_interaction, run_command


class _ScriptedResponse:
    def __init__(self, data: Any) -> None:
        self._data = data

    async def json(self) -> Any:
        return self._data


class _ScriptedSession:
    def __init__(self, data: Any) -> None:
        self._data = data

    async def __aenter__(self) -> "_ScriptedSession":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def get(self, *_a: Any, **_k: Any) -> _ScriptedResponse:
        return _ScriptedResponse(self._data)


def _field(embed: Embed, name: str) -> str:
    return next(str(f.value) for f in embed.fields if f.name == name)


class TestLatestRelease:
    async def test_picks_first_stable_and_prerelease(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            aiohttp,
            "ClientSession",
            lambda *a, **k: _ScriptedSession(
                [
                    {"tag_name": "v2.0.0-rc1", "prerelease": True},
                    {"tag_name": "v1.5.0", "prerelease": False},
                    {"tag_name": "v1.4.0", "prerelease": False},
                ]
            ),
        )
        cog = Releases(make_bot())
        embed = await run_command(cog, "latest_release", make_interaction())
        # Stable scan stops at the first non-prerelease (v1.5.0); the pre-release
        # is the newest one seen before it.
        assert "v1.5.0" in _field(embed, "Latest Release")
        assert "v2.0.0-rc1" in _field(embed, "Latest Pre-release")

    async def test_no_stable_release_shows_na(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            aiohttp,
            "ClientSession",
            lambda *a, **k: _ScriptedSession(
                [{"tag_name": "v3.0.0-beta", "prerelease": True}]
            ),
        )
        cog = Releases(make_bot())
        embed = await run_command(cog, "latest_release", make_interaction())
        assert _field(embed, "Latest Release") == "N/A"
        assert "v3.0.0-beta" in _field(embed, "Latest Pre-release")
