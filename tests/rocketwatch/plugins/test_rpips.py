from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from rocketwatch.plugins.rpips import rpips as rpips_module
from rocketwatch.plugins.rpips.rpips import RPIPs
from tests.lib.discord_harness import make_bot, make_interaction


class _FakeResp:
    def __init__(self, text: str) -> None:
        self._text = text

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def text(self) -> str:
        return self._text


class _FakeSession:
    def __init__(self, text: str) -> None:
        self._text = text

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    def get(self, *_a: Any, **_k: Any) -> _FakeResp:
        return _FakeResp(self._text)


def _patch_http(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(
        rpips_module.aiohttp, "ClientSession", lambda *a, **k: _FakeSession(text)
    )


@pytest.fixture(autouse=True)
async def _clear_caches() -> AsyncIterator[None]:
    # get_all_rpips / fetch_details use module-level aiocache; clear between
    # tests so cached HTML from one test doesn't leak into the next.
    await RPIPs.get_all_rpips.cache.clear()  # type: ignore[attr-defined]
    await RPIPs.RPIP.fetch_details.cache.clear()  # type: ignore[attr-defined]
    yield


INDEX_HTML = """
<table>
  <tr><td class="title">First Thing</td><td class="rpipnum">1</td>
      <td class="status">Active</td></tr>
  <tr><td class="title">Second Thing</td><td class="rpipnum">42</td>
      <td class="status">Draft</td></tr>
  <tr><td>malformed row, skipped</td></tr>
</table>
"""

DETAIL_HTML = """
<main>
  <table class="rpip-preamble">
    <tr><th>Type</th><td>Standard</td></tr>
    <tr><th>Author</th><td><a>Alice</a><a>Bob</a></td></tr>
    <tr><th>Created</th><td>2024-01-01</td></tr>
    <tr><th>Discussion</th><td><a href="https://forum/x">thread</a></td></tr>
  </table>
  <big class="rpip-description">A concise summary.</big>
</main>
"""


class TestRpipProperties:
    def test_full_title_and_url(self) -> None:
        rpip = RPIPs.RPIP("My Title", 7, "Active")
        assert rpip.full_title == "RPIP-7: My Title"
        assert rpip.url == "https://rpips.rocketpool.net/RPIPs/RPIP-7"
        assert str(rpip) == "RPIP-7: My Title"


class TestGetAllRpips:
    async def test_parses_index_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_http(monkeypatch, INDEX_HTML)
        rpips = await RPIPs.get_all_rpips()
        assert [(r.number, r.title, r.status) for r in rpips] == [
            (1, "First Thing", "Active"),
            (42, "Second Thing", "Draft"),
        ]

    async def test_no_table_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_http(monkeypatch, "<html><body>nothing</body></html>")
        assert await RPIPs.get_all_rpips() == []


class TestFetchDetails:
    async def test_parses_preamble_and_description(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_http(monkeypatch, DETAIL_HTML)
        details = await RPIPs.RPIP("T", 1, "Active").fetch_details()
        assert details["type"] == "Standard"
        assert details["authors"] == ["Alice", "Bob"]
        assert details["created"] == "2024-01-01"
        assert details["discussion"] == "https://forum/x"
        assert details["description"] == "A concise summary."

    async def test_missing_preamble_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_http(monkeypatch, "<main><p>no table</p></main>")
        assert await RPIPs.RPIP("T", 2, "Active").fetch_details() == {}


class TestRpipCommand:
    async def test_matching_rpip_renders_details(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rpip = RPIPs.RPIP("First Thing", 1, "Active")
        monkeypatch.setattr(RPIPs, "get_all_rpips", AsyncMock(return_value=[rpip]))
        # RPIP uses __slots__, so fetch_details must be patched on the class.
        monkeypatch.setattr(
            RPIPs.RPIP,
            "fetch_details",
            AsyncMock(
                return_value={
                    "authors": ["Alice"],
                    "created": "2024-01-01",
                    "discussion": "https://forum/x",
                    "description": "A summary.",
                }
            ),
        )

        cog = RPIPs(make_bot())
        interaction = make_interaction()
        await cog.rpip.callback(cog, interaction, name="RPIP-1: First Thing")

        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.title == "RPIP-1: First Thing"
        fields = {f.name: f.value for f in embed.fields}
        assert fields["Author"] == "Alice"
        assert fields["Status"] == "Active"
        assert embed.description == "A summary."

    async def test_multiple_authors_uses_plural_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rpip = RPIPs.RPIP("Thing", 2, "Draft")
        monkeypatch.setattr(RPIPs, "get_all_rpips", AsyncMock(return_value=[rpip]))
        monkeypatch.setattr(
            RPIPs.RPIP,
            "fetch_details",
            AsyncMock(
                return_value={
                    "authors": ["Alice", "Bob"],
                    "created": "x",
                    "discussion": "y",
                    "description": "z",
                }
            ),
        )
        cog = RPIPs(make_bot())
        interaction = make_interaction()
        await cog.rpip.callback(cog, interaction, name="RPIP-2: Thing")
        fields = {
            f.name: f.value
            for f in interaction.followup.send.call_args.kwargs["embed"].fields
        }
        assert fields["Authors"] == "Alice, Bob"

    async def test_unknown_name_reports_no_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(RPIPs, "get_all_rpips", AsyncMock(return_value=[]))
        cog = RPIPs(make_bot())
        interaction = make_interaction()
        await cog.rpip.callback(cog, interaction, name="RPIP-99: Nope")
        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.description == "No matching RPIPs."


class TestAutocomplete:
    async def test_filters_and_caps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rpips = [RPIPs.RPIP(f"Thing {i}", i, "Active") for i in range(30)]
        monkeypatch.setattr(RPIPs, "get_all_rpips", AsyncMock(return_value=rpips))
        cog = RPIPs(make_bot())
        out = await cog._get_rpip_names(make_interaction(), "thing")
        # Capped at 25 results.
        assert len(out) == 25

    async def test_substring_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rpips = [
            RPIPs.RPIP("Staking changes", 1, "Active"),
            RPIPs.RPIP("Governance update", 2, "Draft"),
        ]
        monkeypatch.setattr(RPIPs, "get_all_rpips", AsyncMock(return_value=rpips))
        cog = RPIPs(make_bot())
        out = await cog._get_rpip_names(make_interaction(), "governance")
        assert len(out) == 1
        assert "Governance" in out[0].name
