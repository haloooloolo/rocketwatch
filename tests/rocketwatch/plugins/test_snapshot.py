import time
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from pymongo.asynchronous.database import AsyncDatabase
from web3.constants import ADDRESS_ZERO

from rocketwatch.plugins.snapshot import snapshot as snapshot_module
from rocketwatch.plugins.snapshot.snapshot import Snapshot
from tests.lib.discord_harness import (
    captured_embed,
    make_bot,
    make_interaction,
    run_command,
)
from tests.lib.scripted_rocketpool import ScriptedRocketPool

_FUTURE = int(time.time()) + 1_000_000


class _ScriptedResponse:
    def __init__(self, data: Any) -> None:
        self._data = data

    async def __aenter__(self) -> "_ScriptedResponse":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def json(self) -> Any:
        return self._data


class _ScriptedSession:
    def __init__(self, data: Any) -> None:
        self._data = data

    async def __aenter__(self) -> "_ScriptedSession":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    def get(self, *_a: Any, **_k: Any) -> _ScriptedResponse:
        # snapshot uses `async with session.get(...) as resp`, so get() must
        # return an async context manager rather than a coroutine.
        return _ScriptedResponse(self._data)


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


# ---- GraphQL plumbing ---------------------------------------------------------


class TestQueryApi:
    async def test_unwraps_data_by_query_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graphql_query import Query

        payload = {"data": {"proposal": {"id": "0xabc"}}}
        monkeypatch.setattr(
            aiohttp, "ClientSession", lambda *a, **k: _ScriptedSession(payload)
        )
        result = await Snapshot._query_api(Query(name="proposal", fields=["id"]))
        assert result == {"id": "0xabc"}


class TestFetchProposal:
    async def test_builds_proposal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            Snapshot,
            "_query_api",
            AsyncMock(
                return_value={
                    "id": "0xabc",
                    "title": "T",
                    "choices": ["For"],
                    "start": 1,
                    "end": 2,
                    "scores": [1.0],
                    "quorum": 1,
                }
            ),
        )
        p = await Snapshot.fetch_proposal("0xabc")
        assert p is not None and p.id == "0xabc"

    async def test_empty_response_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Snapshot, "_query_api", AsyncMock(return_value={}))
        assert await Snapshot.fetch_proposal("0xabc") is None


class TestFetchProposals:
    async def test_parses_each_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            Snapshot,
            "_query_api",
            AsyncMock(
                return_value=[
                    {
                        "id": f"0x{i}",
                        "title": "A",
                        "choices": ["For"],
                        "start": 1,
                        "end": 2,
                        "scores": [1.0],
                        "quorum": 1,
                    }
                    for i in (1, 2)
                ]
            ),
        )
        proposals = await Snapshot.fetch_proposals("active")
        assert [p.id for p in proposals] == ["0x1", "0x2"]


class TestFetchVotes:
    async def test_attaches_proposal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proposal = _proposal()
        monkeypatch.setattr(
            Snapshot,
            "_query_api",
            AsyncMock(
                return_value=[
                    {
                        "id": "v1",
                        "voter": "0xaa",
                        "created": 10,
                        "vp": 5.0,
                        "choice": 1,
                        "reason": "",
                    }
                ]
            ),
        )
        votes = await Snapshot.fetch_votes(proposal)
        assert len(votes) == 1
        assert votes[0].id == "v1"
        assert votes[0].proposal is proposal


# ---- Image rendering (render_to) ----------------------------------------------


class TestCreateImage:
    def test_renders_quorum_met_future(self) -> None:
        p = _proposal(
            choices=["For", "Against", "Abstain"],
            scores=[60, 30, 10],
            quorum=100,
            end=_FUTURE,
        )
        assert p.create_image(include_title=True) is not None
        assert p.create_image(include_title=False) is not None

    def test_renders_quorum_unmet_past_many_choices(self) -> None:
        # ≥5 choices uses max() as the bar divisor; past end shows "Final
        # Result"; below quorum exercises the unmet colour branch.
        p = _proposal(
            choices=["A", "B", "C", "D", "E"],
            scores=[1, 1, 1, 1, 1],
            quorum=1000,
            end=1,
        )
        assert p.create_image(include_title=True) is not None


# ---- Proposal lifecycle events ------------------------------------------------


class TestProposalEvents:
    async def test_start_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(snapshot_module, "ts_to_block", AsyncMock(return_value=100))
        ev = await _proposal().create_start_event()
        assert ev.event_name == "pdao_snapshot_vote_start"
        assert ev.image is not None

    async def test_end_event_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(snapshot_module, "ts_to_block", AsyncMock(return_value=100))
        p = _proposal(choices=["For", "Against"], scores=[80, 10], quorum=50)
        ev = await p.create_end_event()
        assert ev.embed.title is not None and "Passed" in ev.embed.title

    async def test_end_event_failed_when_against_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(snapshot_module, "ts_to_block", AsyncMock(return_value=100))
        p = _proposal(choices=["For", "Against"], scores=[10, 80], quorum=50)
        ev = await p.create_end_event()
        assert ev.embed.title is not None and "Failed" in ev.embed.title

    def test_reached_quorum_event(self) -> None:
        ev = _proposal().create_reached_quorum_event(500)  # type: ignore[arg-type]
        assert ev.event_name == "pdao_snapshot_vote_quorum"
        assert ev.block_number == 500


# ---- Vote → Event -------------------------------------------------------------


def _patch_vote_deps(
    monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
) -> None:
    monkeypatch.setattr(snapshot_module, "ts_to_block", AsyncMock(return_value=100))
    monkeypatch.setattr(
        snapshot_module, "el_explorer_url", AsyncMock(return_value="VOTER")
    )
    scripted_rp.set_call("rocketSignerRegistry.signerToNode", lambda _v: ADDRESS_ZERO)


class TestVoteCreateEvent:
    async def test_new_high_vp_vote_uses_image(
        self, monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
    ) -> None:
        _patch_vote_deps(monkeypatch, scripted_rp)
        ev = await _vote(vp=300.0, choice=1).create_event(None)
        assert ev is not None
        assert ev.event_name == "pdao_snapshot_vote"
        assert ev.image is not None

    async def test_low_vp_vote_uses_thumbnail(
        self, monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
    ) -> None:
        _patch_vote_deps(monkeypatch, scripted_rp)
        ev = await _vote(vp=10.0, choice=1).create_event(None)
        assert ev is not None
        assert ev.event_name == "snapshot_vote"
        assert ev.thumbnail is not None

    async def test_unchanged_vote_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
    ) -> None:
        _patch_vote_deps(monkeypatch, scripted_rp)
        prev = _vote(choice=1, reason="x")
        assert await _vote(choice=1, reason="x").create_event(prev) is None

    async def test_changed_choice_describes_switch(
        self, monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
    ) -> None:
        _patch_vote_deps(monkeypatch, scripted_rp)
        prev = _vote(choice=1)
        ev = await _vote(choice=2, vp=300.0).create_event(prev)
        assert ev is not None
        assert ev.embed.description is not None
        assert "changed their vote" in ev.embed.description

    async def test_changed_reason_describes_reason(
        self, monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
    ) -> None:
        _patch_vote_deps(monkeypatch, scripted_rp)
        prev = _vote(choice=1, reason="old", vp=300.0)
        ev = await _vote(choice=1, reason="new", vp=300.0).create_event(prev)
        assert ev is not None
        assert ev.embed.description is not None
        assert "changed the reason" in ev.embed.description

    async def test_overlong_reason_is_truncated(
        self, monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
    ) -> None:
        _patch_vote_deps(monkeypatch, scripted_rp)
        ev = await _vote(vp=300.0, choice=1, reason="x" * 2100).create_event(None)
        assert ev is not None
        assert ev.embed.description is not None
        assert ev.embed.description.endswith("...```")


# ---- _get_new_events ----------------------------------------------------------


class TestGetNewEvents:
    async def test_new_proposal_emits_start_and_persists(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(snapshot_module, "ts_to_block", AsyncMock(return_value=100))
        p = _proposal(proposal_id="0xnew", end=_FUTURE)
        monkeypatch.setattr(Snapshot, "fetch_proposals", AsyncMock(return_value=[p]))
        monkeypatch.setattr(Snapshot, "fetch_votes", AsyncMock(return_value=[]))

        cog = Snapshot(make_bot(db=mongo_db))
        events = await cog._get_new_events()

        assert any(e.event_name == "pdao_snapshot_vote_start" for e in events)
        assert await mongo_db.snapshot_proposals.find_one({"_id": "0xnew"}) is not None

    async def test_expired_proposal_emits_end_and_deletes(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(snapshot_module, "ts_to_block", AsyncMock(return_value=100))
        await mongo_db.snapshot_proposals.insert_one(
            {"_id": "0xold", "start": 1, "end": 2, "quorum": True}
        )
        p = _proposal(proposal_id="0xold", end=2)
        monkeypatch.setattr(Snapshot, "fetch_proposal", AsyncMock(return_value=p))
        monkeypatch.setattr(Snapshot, "fetch_proposals", AsyncMock(return_value=[]))

        cog = Snapshot(make_bot(db=mongo_db))
        events = await cog._get_new_events()

        assert any(e.event_name == "pdao_snapshot_vote_end" for e in events)
        assert await mongo_db.snapshot_proposals.find_one({"_id": "0xold"}) is None

    async def test_vote_emits_event_and_persists(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        _patch_vote_deps(monkeypatch, scripted_rp)
        p = _proposal(proposal_id="0xactive", end=_FUTURE)
        await mongo_db.snapshot_proposals.insert_one(
            {"_id": "0xactive", "start": 1, "end": p.end, "quorum": True}
        )
        v = _vote(proposal=p, vp=300.0, choice=1)
        monkeypatch.setattr(Snapshot, "fetch_proposals", AsyncMock(return_value=[p]))
        monkeypatch.setattr(Snapshot, "fetch_votes", AsyncMock(return_value=[v]))

        cog = Snapshot(make_bot(db=mongo_db))
        events = await cog._get_new_events()

        assert any(
            e.event_name in ("pdao_snapshot_vote", "snapshot_vote") for e in events
        )
        assert await mongo_db.snapshot_votes.find_one({"voter": v.voter}) is not None

    async def test_known_proposal_reaching_quorum_emits_event(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(snapshot_module, "ts_to_block", AsyncMock(return_value=100))
        # Stored as not-yet-quorum; the freshly fetched proposal has reached it.
        p = _proposal(proposal_id="0xq", scores=[60, 30, 10], quorum=100, end=_FUTURE)
        await mongo_db.snapshot_proposals.insert_one(
            {"_id": "0xq", "start": 1, "end": p.end, "quorum": False}
        )
        monkeypatch.setattr(Snapshot, "fetch_proposals", AsyncMock(return_value=[p]))
        monkeypatch.setattr(Snapshot, "fetch_votes", AsyncMock(return_value=[]))

        cog = Snapshot(make_bot(db=mongo_db))
        events = await cog._get_new_events()

        assert any(e.event_name == "pdao_snapshot_vote_quorum" for e in events)
        doc = await mongo_db.snapshot_proposals.find_one({"_id": "0xq"})
        assert doc is not None and doc["quorum"] is True


# ---- snapshot_votes command: image branch -------------------------------------


class TestSnapshotVotesImage:
    async def test_renders_grid_for_active_proposals(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            Snapshot, "fetch_proposals", AsyncMock(return_value=[_proposal()])
        )
        cog = Snapshot(make_bot())
        interaction = make_interaction()
        await run_command(cog, "snapshot_votes", interaction)
        embed = captured_embed(interaction)
        assert embed.image.url is not None and embed.image.url.startswith(
            "attachment://"
        )
        assert "file" in interaction.followup.send.call_args.kwargs
