from typing import cast
from unittest.mock import AsyncMock

import pytest
from eth_typing import HexStr

from rocketwatch.plugins.governance.governance import Governance
from rocketwatch.utils.dao import DefaultDAO, ProtocolDAO
from tests.lib.discord_harness import (
    make_bot,
    make_interaction,
    run_command,
)


def _odao_proposal(*, proposal_id: int, message: str) -> DefaultDAO.Proposal:
    return DefaultDAO.Proposal(
        id=proposal_id,
        proposer="0x" + "00" * 20,  # type: ignore[arg-type]
        message=message,
        payload=b"",
        created=1_700_000_000,
        start=0,
        end=0,
        expires=0,
        votes_for=0,
        votes_against=0,
        votes_required=0.0,
    )


def _pdao_proposal(*, proposal_id: int, message: str) -> ProtocolDAO.Proposal:
    return ProtocolDAO.Proposal(
        id=proposal_id,
        proposer="0x" + "00" * 20,  # type: ignore[arg-type]
        message=message,
        payload=b"",
        created=1_700_000_000,
        start=0,
        end_phase_1=0,
        end_phase_2=0,
        expires=0,
        votes_for=0.0,
        votes_against=0.0,
        votes_veto=0.0,
        votes_abstain=0.0,
        quorum=1.0,
        veto_quorum=1.0,
    )


@pytest.fixture
def stub_collaborators(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every external collaborator to "no data". Individual tests
    override what they care about."""
    monkeypatch.setattr(
        "rocketwatch.plugins.governance.governance.SecurityCouncil",
        lambda: object(),
    )
    monkeypatch.setattr(
        "rocketwatch.plugins.governance.governance.OracleDAO",
        lambda: object(),
    )
    monkeypatch.setattr(
        "rocketwatch.plugins.governance.governance.ProtocolDAO",
        lambda: object(),
    )
    monkeypatch.setattr(
        Governance,
        "_get_active_dao_proposals",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        Governance,
        "_get_active_pdao_proposals",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        Governance,
        "_get_tx_hash_for_proposal",
        AsyncMock(return_value=cast(HexStr, "0x" + "ab" * 32)),
    )


@pytest.fixture
def cog(monkeypatch: pytest.MonkeyPatch, stub_collaborators: None) -> Governance:
    bot = make_bot()
    cog_instance = Governance(bot)
    # Make every async helper a no-data default; tests override per-instance.
    cog_instance._get_active_snapshot_proposals = AsyncMock(return_value=[])  # type: ignore[method-assign]
    cog_instance._get_draft_rpips = AsyncMock(return_value=[])  # type: ignore[method-assign]
    cog_instance._get_latest_forum_topics = AsyncMock(return_value=[])  # type: ignore[method-assign]
    return cog_instance


class TestGovernanceDigest:
    async def test_no_data_shows_tenor_gif(self, cog: Governance) -> None:
        embed = await cog.get_digest()
        # Empty digest falls through to the "nothing happening" placeholder.
        assert embed.description == ""
        assert embed.image.url is not None
        assert embed.image.url.startswith("https://c.tenor.com/")

    async def test_security_council_section_renders_when_proposals_exist(
        self, cog: Governance, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # _get_active_dao_proposals is called twice (SC and oDAO). Use a
        # side-effect list so only the first call returns proposals.
        sc_proposals = [_odao_proposal(proposal_id=42, message="Funding round X")]
        monkeypatch.setattr(
            Governance,
            "_get_active_dao_proposals",
            AsyncMock(side_effect=[sc_proposals, []]),
        )
        embed = await cog.get_digest()
        assert "Security Council" in (embed.description or "")
        assert "Funding round X" in (embed.description or "")
        # `(#42)` is the proposal ID suffix.
        assert "(#42)" in (embed.description or "")

    async def test_oracle_dao_section_renders_after_security_council(
        self, cog: Governance, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        odao_proposals = [_odao_proposal(proposal_id=7, message="Add new oDAO")]
        monkeypatch.setattr(
            Governance,
            "_get_active_dao_proposals",
            AsyncMock(side_effect=[[], odao_proposals]),
        )
        embed = await cog.get_digest()
        # Should appear under Oracle DAO header, not Security Council.
        assert "Oracle DAO" in (embed.description or "")
        assert "Security Council" not in (embed.description or "")
        assert "Add new oDAO" in (embed.description or "")

    async def test_pdao_section_aggregates_three_sources(
        self,
        cog: Governance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # On-chain pDAO proposals + Snapshot proposals + draft RPIPs all
        # render under the same Protocol DAO header.
        monkeypatch.setattr(
            Governance,
            "_get_active_pdao_proposals",
            AsyncMock(return_value=[_pdao_proposal(proposal_id=1, message="On-chain")]),
        )

        # Snapshot.Proposal is exposed as a small dataclass in the snapshot
        # plugin; here we just need title + url attributes.
        from types import SimpleNamespace

        cog._get_active_snapshot_proposals = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                SimpleNamespace(title="Snap title", url="https://snap.example/1")
            ]
        )
        cog._get_draft_rpips = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                SimpleNamespace(
                    title="RPIP-99 title",
                    url="https://rpips.example/99",
                    number=99,
                )
            ]
        )

        embed = await cog.get_digest()
        desc = embed.description or ""
        assert "### Protocol DAO" in desc
        assert "On-chain" in desc
        assert "Snap title" in desc
        assert "RPIP-99" in desc

    async def test_forum_section_appears_when_topics_recent(
        self, cog: Governance
    ) -> None:
        from types import SimpleNamespace

        cog._get_latest_forum_topics = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                SimpleNamespace(
                    title="Hot Take",
                    url="https://forum.example/t/1",
                    post_count=10,
                )
            ]
        )
        embed = await cog.get_digest()
        desc = embed.description or ""
        assert "### Forum" in desc
        assert "Hot Take" in desc
        # Post count rendered as `post_count - 1` (reply count vs total).
        assert "9" in desc

    async def test_sanitize_truncates_long_titles(
        self, cog: Governance, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        long_title = "x" * 200
        monkeypatch.setattr(
            Governance,
            "_get_active_dao_proposals",
            AsyncMock(
                side_effect=[[_odao_proposal(proposal_id=1, message=long_title)], []]
            ),
        )
        embed = await cog.get_digest()
        # The sanitize() inside print_proposals caps at 40 chars + ellipsis.
        # The ellipsis character "…" should appear in the rendered output.
        assert "…" in (embed.description or "")


class TestGovernanceCommands:
    async def test_governance_digest_command_sends_embed(self, cog: Governance) -> None:
        interaction = make_interaction()
        embed = await run_command(cog, "governance_digest", interaction)
        # No-data path is fine for this test — just confirm the command path
        # produces an embed via the followup.send route.
        assert embed.title == "Governance Digest"

    async def test_get_status_changes_title(self, cog: Governance) -> None:
        # The status-feed path reuses the digest body but rewrites the title
        # with a live-update marker.
        embed = await cog.get_status()
        assert embed.title is not None
        assert "Live Governance" in embed.title


class TestGovernanceErrorPaths:
    async def test_snapshot_error_swallowed_and_reported(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_collaborators: None,
    ) -> None:
        # When `Snapshot.fetch_proposals` raises, `_get_active_snapshot_proposals`
        # must return [] and forward the error to bot.report_error.
        from rocketwatch.plugins.governance import governance as mod

        monkeypatch.setattr(
            mod.Snapshot,
            "fetch_proposals",
            AsyncMock(side_effect=RuntimeError("snapshot down")),
        )

        bot = make_bot()
        cog = Governance(bot)
        result = await cog._get_active_snapshot_proposals()
        assert result == []
        assert cog.bot.report_error.await_count == 1

    async def test_rpips_error_swallowed_and_reported(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_collaborators: None,
    ) -> None:
        from rocketwatch.plugins.governance import governance as mod

        monkeypatch.setattr(
            mod.RPIPs,
            "get_all_rpips",
            AsyncMock(side_effect=RuntimeError("rpips down")),
        )
        bot = make_bot()
        cog = Governance(bot)
        result = await cog._get_draft_rpips()
        assert result == []
        assert cog.bot.report_error.await_count == 1

    async def test_forum_error_swallowed_and_reported(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_collaborators: None,
    ) -> None:
        from rocketwatch.plugins.governance import governance as mod

        monkeypatch.setattr(
            mod.Forum,
            "get_recent_topics",
            AsyncMock(side_effect=RuntimeError("forum down")),
        )
        bot = make_bot()
        cog = Governance(bot)
        result = await cog._get_latest_forum_topics(days=7)
        assert result == []
        assert cog.bot.report_error.await_count == 1
