from unittest.mock import AsyncMock

import pytest

from rocketwatch.plugins.snapshot.snapshot import Snapshot
from tests.lib.discord_harness import (
    captured_embed,
    make_bot,
    make_interaction,
    run_command,
)


def _proposal(
    *,
    proposal_id: str = "0xprop",
    title: str = "Test Proposal",
    choices: list[str] | None = None,
    start: int = 1_700_000_000,
    end: int = 1_710_000_000,
    scores: list[float] | None = None,
    quorum: int = 100,
) -> Snapshot.Proposal:
    return Snapshot.Proposal(
        id=proposal_id,
        title=title,
        choices=choices or ["For", "Against", "Abstain"],
        start=start,
        end=end,
        scores=scores or [60.0, 30.0, 10.0],
        quorum=quorum,
    )


def _vote(
    *,
    proposal: Snapshot.Proposal | None = None,
    voter: str = "0x" + "11" * 20,
    vp: float = 100.0,
    choice: Snapshot.Choice = 1,
    reason: str = "",
    created: int = 1_700_000_500,
) -> Snapshot.Vote:
    return Snapshot.Vote(
        proposal=proposal or _proposal(),
        id=f"vote-{voter}",
        voter=voter,  # type: ignore[arg-type]
        created=created,
        vp=vp,
        choice=choice,
        reason=reason,
    )


# ---- Proposal: pure ------------------------------------------------------------


class TestProposalQuorum:
    def test_quorum_met_when_sum_exceeds_threshold(self) -> None:
        p = _proposal(scores=[60, 30, 10], quorum=100)
        assert p.reached_quorum() is True

    def test_quorum_met_at_threshold_inclusive(self) -> None:
        p = _proposal(scores=[100], quorum=100)
        assert p.reached_quorum() is True

    def test_quorum_not_met_below_threshold(self) -> None:
        p = _proposal(scores=[50, 30, 10], quorum=100)
        assert p.reached_quorum() is False


class TestProposalUrl:
    def test_includes_id_in_path(self) -> None:
        url = _proposal(proposal_id="0xabc").url
        assert url == "https://vote.rocketpool.net/#/proposal/0xabc"


class TestProposalRenderHeight:
    def test_increases_with_choice_count(self) -> None:
        # More choices → taller render.
        small = _proposal(choices=["For", "Against"], scores=[1, 1])
        large = _proposal(choices=["A", "B", "C", "D", "E"], scores=[1, 1, 1, 1, 1])
        assert large.predict_render_height() > small.predict_render_height()

    def test_with_title_taller_than_without(self) -> None:
        p = _proposal()
        assert p.predict_render_height(with_title=True) > p.predict_render_height(
            with_title=False
        )


class TestProposalEmbedTemplate:
    def test_sets_author_with_url(self) -> None:
        p = _proposal(proposal_id="0xprop")
        embed = p.get_embed_template()
        # Author block exists and points to the proposal page.
        assert embed.author.name == "🔗 Data from snapshot.org"
        assert embed.author.url == p.url


# ---- Vote: pretty_print formatting --------------------------------------------


class TestVoteFormatSingleChoice:
    def test_for_choice_gets_check_emoji(self) -> None:
        v = _vote(choice=1)
        assert v.pretty_print() == "`✅ For`"

    def test_against_choice_gets_x_emoji(self) -> None:
        v = _vote(choice=2)
        assert v.pretty_print() == "`❌ Against`"

    def test_abstain_choice_gets_circle_emoji(self) -> None:
        v = _vote(choice=3)
        assert v.pretty_print() == "`⚪ Abstain`"

    def test_custom_choice_passes_through_unchanged(self) -> None:
        # An unrecognised choice label is wrapped in backticks but not
        # decorated with any emoji.
        p = _proposal(choices=["Yes", "Maybe"], scores=[10, 5])
        v = _vote(proposal=p, choice=2)
        assert v.pretty_print() == "`Maybe`"


class TestVoteFormatMultiChoice:
    def test_single_element_list_renders_as_single(self) -> None:
        v = _vote(choice=[1])
        assert v.pretty_print() == "`For`"

    def test_multiple_elements_render_as_bold_list(self) -> None:
        v = _vote(choice=[1, 2])
        out = v.pretty_print() or ""
        assert "**" in out
        assert "- For" in out
        assert "- Against" in out


class TestVoteFormatWeightedChoice:
    def test_renders_weighted_bar_chart(self) -> None:
        # Two-choice weighted vote: 60% For, 40% Against.
        v = _vote(choice={"1": 60, "2": 40})
        out = v.pretty_print() or ""
        # Wrapped in code fences for Discord monospace rendering.
        assert out.startswith("```")
        assert out.endswith("```")
        assert "For" in out
        assert "Against" in out
        # The `]` from termplotlib's bar bracket is replaced with `%]`.
        assert "%]" in out


class TestVoteFormatUnknownType:
    def test_returns_none_for_unsupported_choice_type(self) -> None:
        # Construct a Vote with an unsupported choice (e.g. None) and ensure
        # the function returns None rather than raising.
        v = Snapshot.Vote(
            proposal=_proposal(),
            id="vote-x",
            voter="0xabc",  # type: ignore[arg-type]
            created=0,
            vp=0,
            choice=None,  # type: ignore[arg-type]
            reason="",
        )
        assert v.pretty_print() is None


# ---- snapshot_votes command: empty branch -------------------------------------


class TestSnapshotVotesCommand:
    async def test_no_active_proposals_returns_no_proposals_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When `fetch_proposals` returns []  the cog short-circuits with a
        # plain "No active proposals." embed — no image/canvas.
        monkeypatch.setattr(Snapshot, "fetch_proposals", AsyncMock(return_value=[]))
        cog = Snapshot(make_bot())
        interaction = make_interaction()
        await run_command(cog, "snapshot_votes", interaction)
        embed = captured_embed(interaction)
        assert embed.description == "No active proposals."
        # No file attached on the empty branch.
        call_kwargs = interaction.followup.send.call_args.kwargs
        assert "file" not in call_kwargs
