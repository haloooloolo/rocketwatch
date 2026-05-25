from typing import Any

import aiohttp
import pytest

from rocketwatch.plugins.ask_doofus.ask_doofus import AskDoofus
from tests.lib.discord_harness import make_bot, make_interaction, run_command


class _ScriptedResponse:
    def __init__(self, data: Any) -> None:
        self._data = data

    async def __aenter__(self) -> "_ScriptedResponse":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> Any:
        return self._data


class _ScriptedSession:
    def __init__(self, data: Any) -> None:
        self._data = data

    async def __aenter__(self) -> "_ScriptedSession":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    def post(self, *_a: Any, **_k: Any) -> _ScriptedResponse:
        return _ScriptedResponse(self._data)


class TestAskDoofus:
    async def test_renders_answer_with_sources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            aiohttp,
            "ClientSession",
            lambda *a, **k: _ScriptedSession(
                {
                    "finalAnswer": "Stake your RPL.",
                    "citations": [
                        {
                            "tag": "1",
                            "url": "https://docs/x",
                            "title": "Staking",
                            "heading": "How",
                        }
                    ],
                }
            ),
        )
        cog = AskDoofus(make_bot())
        embed = await run_command(
            cog, "ask_doofus", make_interaction(), "how do I stake?"
        )
        assert embed.description is not None
        assert "> how do I stake?" in embed.description
        assert "Stake your RPL." in embed.description
        assert "**Sources:**" in embed.description
        assert "[1](https://docs/x)" in embed.description

    async def test_missing_answer_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            aiohttp, "ClientSession", lambda *a, **k: _ScriptedSession({})
        )
        cog = AskDoofus(make_bot())
        embed = await run_command(cog, "ask_doofus", make_interaction(), "anything?")
        assert embed.description is not None
        assert "No answer received." in embed.description
        assert "**Sources:**" not in embed.description
