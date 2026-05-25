from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from discord import Embed

from rocketwatch.plugins.about import about as about_module
from rocketwatch.plugins.about.about import About
from tests.lib.discord_harness import (
    captured_embed,
    make_bot,
    make_interaction,
    run_command,
)


class _ScriptedResponse:
    def __init__(self, data: Any, *, raise_on_json: bool = False) -> None:
        self._data = data
        self._raise = raise_on_json

    async def __aenter__(self) -> "_ScriptedResponse":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def json(self) -> Any:
        if self._raise:
            raise RuntimeError("boom")
        return self._data


class _ScriptedSession:
    def __init__(self, data: Any, *, raise_on_json: bool = False) -> None:
        self._data = data
        self._raise = raise_on_json

    async def __aenter__(self) -> "_ScriptedSession":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    def get(self, *_a: Any, **_k: Any) -> _ScriptedResponse:
        return _ScriptedResponse(self._data, raise_on_json=self._raise)


def _field(embed: Embed, name: str) -> str:
    return next(str(f.value) for f in embed.fields if f.name == name)


def _make_cog(bot: Any) -> About:
    # Set on the (MagicMock) bot before constructing the cog; `guilds`/`cogs`
    # are read-only typed properties on the real RocketWatch.
    bot.guilds = [
        SimpleNamespace(member_count=100),
        SimpleNamespace(member_count=50),
    ]
    bot.cogs = {"A": object(), "B": object()}
    return About(bot)


class TestAboutCommand:
    async def test_renders_stats_and_contributors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            about_module, "el_explorer_url", AsyncMock(return_value="STORAGE_LINK")
        )
        monkeypatch.setattr(
            aiohttp,
            "ClientSession",
            lambda *a, **k: _ScriptedSession(
                [
                    {
                        "login": "alice",
                        "html_url": "https://gh/alice",
                        "contributions": 42,
                    },
                    {
                        "login": "somebot",
                        "html_url": "https://gh/somebot",
                        "contributions": 99,
                    },
                ]
            ),
        )

        cog = _make_cog(make_bot())
        embed = await run_command(cog, "about", make_interaction())

        assert _field(embed, "Chain") == "Mainnet"
        assert _field(embed, "Storage Contract") == "STORAGE_LINK"
        assert "2 guilds" in _field(embed, "Bot Statistics")
        assert "150 members" in _field(embed, "Bot Statistics")
        contributors = _field(embed, "Contributors")
        assert "alice" in contributors
        # Logins containing "bot" are filtered out.
        assert "somebot" not in contributors

    async def test_contributor_fetch_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            about_module, "el_explorer_url", AsyncMock(return_value="STORAGE_LINK")
        )
        monkeypatch.setattr(
            aiohttp,
            "ClientSession",
            lambda *a, **k: _ScriptedSession(None, raise_on_json=True),
        )

        bot = make_bot()
        cog = _make_cog(bot)
        interaction = make_interaction()
        await run_command(cog, "about", interaction)
        embed = captured_embed(interaction)

        bot.report_error.assert_awaited()
        assert "Contributors" not in [f.name for f in embed.fields]
