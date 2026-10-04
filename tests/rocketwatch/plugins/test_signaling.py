import time
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from pymongo.asynchronous.database import AsyncDatabase
from web3 import Web3

from rocketwatch.plugins.signaling import signaling as signaling_module
from rocketwatch.plugins.signaling.signaling import (
    Choice,
    Proposal,
    Signaling,
    Vote,
)
from rocketwatch.utils.config import cfg
from tests.lib.discord_harness import (
    captured_embed,
    make_bot,
    make_interaction,
    run_command,
)

_FUTURE = int(time.time()) + 1_000_000
_NODE = "0x" + "11" * 20
_SIGNER = "0x" + "22" * 20


class _ScriptedResponse:
    def __init__(self, data: Any) -> None:
        self._data = data

    async def __aenter__(self) -> "_ScriptedResponse":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    def raise_for_status(self) -> None:
        pass

    async def json(self) -> Any:
        return self._data


class _ScriptedSession:
    def __init__(self, data: Any) -> None:
        self._data = data
        self.requests: list[tuple[str, dict[str, str]]] = []

    async def __aenter__(self) -> "_ScriptedSession":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    def get(self, url: str, params: dict[str, str]) -> _ScriptedResponse:
        self.requests.append((url, dict(params)))
        return _ScriptedResponse(self._data)


def _set_chain(monkeypatch: pytest.MonkeyPatch, chain: str) -> None:
    instance = cfg._instance.model_copy(deep=True)
    instance.rocketpool.chain = chain
    monkeypatch.setattr(cfg, "_instance", instance)


def _proposal(
    *,
    proposal_id: str = "0xprop",
    title: str = "Test Proposal",
    vote_type: str = "basic",
    choices: list[str] | None = None,
    start: int = 1_700_000_000,
    end: int = _FUTURE,
    state: str = "active",
    lifecycle: str = "ready",
    scores: list[float] | None = None,
    quorum: float = 100,
) -> Proposal:
    scores = scores or [60.0, 30.0, 10.0]
    return Proposal(
        id=proposal_id,
        title=title,
        vote_type=vote_type,
        choices=choices or ["For", "Against", "Abstain"],
        start=start,
        end=end,
        state=state,
        lifecycle=lifecycle,
        scores=scores,
        scores_total=sum(scores),
        quorum=quorum,
    )


def _proposal_json(p: Proposal) -> dict[str, Any]:
    return {
        "proposal_id": p.id,
        "title": p.title,
        "vote_type": p.vote_type,
        "choices": p.choices,
        "start_time": p.start,
        "end_time": p.end,
        "snapshot_block": 1,
        "lifecycle": p.lifecycle,
        "state": p.state,
        "author": "0x" + "33" * 20,
        "quorum": p.quorum,
        "power_total": 1000.0,
        "scores": p.scores,
        "scores_total": p.scores_total,
        "vote_count": 0,
    }


def _vote_json(
    *,
    node: str = _NODE,
    choice: Choice = 1,
    reason: str = "",
    timestamp: int = 1_700_000_500,
    power: float = 100.0,
    is_override: bool = False,
) -> dict[str, Any]:
    return {
        "node": node,
        "signer": _SIGNER,
        "name": None,
        "choice": choice,
        "reason": reason,
        "timestamp": timestamp,
        "effective_power": power,
        "is_override": is_override,
    }


def _vote(*, proposal: Proposal | None = None, **kwargs: Any) -> Vote:
    return Vote.from_api(proposal or _proposal(), _vote_json(**kwargs))


class _ScriptedApi:
    """Stands in for the RocketDash API, keyed by endpoint."""

    def __init__(self) -> None:
        self.proposals: list[Proposal] = []
        self.votes: dict[str, list[dict[str, Any]]] = {}
        self.discussion_url = ""

    async def __call__(self, endpoint: str, **params: str) -> Any:
        match endpoint:
            case "proposals":
                return {"proposals": [_proposal_json(p) for p in self.proposals]}
            case "proposal-detail":
                return {
                    "proposal_id": params["id"],
                    "discussion_url": self.discussion_url,
                }
            case "proposal-votes":
                return {"votes": self.votes.get(params["id"], [])}
        raise AssertionError(f"unexpected endpoint {endpoint}")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> _ScriptedApi:
    scripted = _ScriptedApi()
    monkeypatch.setattr(signaling_module, "_query_api", scripted)
    monkeypatch.setattr(signaling_module, "ts_to_block", AsyncMock(return_value=100))
    monkeypatch.setattr(
        signaling_module, "el_explorer_url", AsyncMock(side_effect=lambda a: f"[{a}]")
    )
    return scripted


# ---- Proposal: pure ------------------------------------------------------------


class TestProposalFromApi:
    def test_parses_rocketdash_fields(self) -> None:
        p = _proposal(proposal_id="0xabc", vote_type="weighted", quorum=50)
        assert Proposal.from_api(_proposal_json(p)) == p


class TestProposalIsActive:
    def test_active_and_ready_before_end(self) -> None:
        assert _proposal().is_active() is True

    def test_closed_state_is_inactive(self) -> None:
        assert _proposal(state="closed").is_active() is False

    def test_not_ready_is_inactive(self) -> None:
        assert _proposal(lifecycle="pending").is_active() is False

    def test_past_end_is_inactive(self) -> None:
        assert _proposal(end=1).is_active() is False


class TestProposalQuorum:
    def test_quorum_met_when_total_exceeds_threshold(self) -> None:
        assert _proposal(scores=[60, 30, 10], quorum=90).reached_quorum() is True

    def test_quorum_met_at_threshold_inclusive(self) -> None:
        assert _proposal(scores=[100], quorum=100).reached_quorum() is True

    def test_quorum_not_met_below_threshold(self) -> None:
        assert _proposal(scores=[50, 30, 10], quorum=100).reached_quorum() is False


class TestProposalUrl:
    def test_mainnet_has_no_network_param(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_chain(monkeypatch, "mainnet")
        url = _proposal(proposal_id="0xabc").url
        assert url == "https://rocketdash.net/vote/0xabc"

    def test_testnet_adds_network_param(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_chain(monkeypatch, "hoodi")
        url = _proposal(proposal_id="0xabc").url
        assert url == "https://rocketdash.net/vote/0xabc?network=hoodi"


class TestProposalRenderHeight:
    def test_increases_with_choice_count(self) -> None:
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
        p = _proposal()
        embed = p.get_embed_template()
        assert embed.author.name == "🔗 Data from rocketdash.net/vote"
        assert embed.author.url == p.url


# ---- Vote: pretty_print formatting --------------------------------------------


class TestVoteFromApi:
    def test_checksums_addresses(self) -> None:
        node = "0x" + "ab" * 20
        v = _vote(node=node)
        assert v.node == Web3.to_checksum_address(node)
        assert v.signer == _SIGNER


class TestVoteFormatSingleChoice:
    def test_for_choice_gets_check_emoji(self) -> None:
        assert _vote(choice=1).pretty_print() == "`✅ For`"

    def test_against_choice_gets_x_emoji(self) -> None:
        assert _vote(choice=2).pretty_print() == "`❌ Against`"

    def test_abstain_choice_gets_circle_emoji(self) -> None:
        assert _vote(choice=3).pretty_print() == "`⚪ Abstain`"

    def test_custom_choice_passes_through_unchanged(self) -> None:
        p = _proposal(choices=["Yes", "Maybe"], scores=[10, 5])
        assert _vote(proposal=p, choice=2).pretty_print() == "`Maybe`"


class TestVoteFormatMultiChoice:
    def test_single_element_list_renders_as_single(self) -> None:
        assert _vote(choice=[1]).pretty_print() == "`For`"

    def test_ranked_choice_renders_numbered_in_rank_order(self) -> None:
        p = _proposal(vote_type="ranked-choice", choices=["A", "B", "C"])
        out = _vote(proposal=p, choice=[3, 1, 2]).pretty_print() or ""
        assert "1. C\n2. A\n3. B" in out

    def test_unranked_multi_choice_renders_as_bullets(self) -> None:
        p = _proposal(vote_type="approval")
        out = _vote(proposal=p, choice=[1, 2]).pretty_print() or ""
        assert "- For" in out
        assert "- Against" in out


class TestVoteFormatWeightedChoice:
    def test_renders_weighted_bar_chart(self) -> None:
        out = _vote(choice={"1": 60, "2": 40}).pretty_print() or ""
        assert out.startswith("```")
        assert out.endswith("```")
        assert "For" in out
        assert "Against" in out
        assert "%]" in out


class TestVoteFormatUnknownType:
    def test_returns_none_for_unsupported_choice_type(self) -> None:
        assert _vote(choice=None).pretty_print() is None


class TestVoteDbRoundTrip:
    def test_from_db_restores_vote(self) -> None:
        p = _proposal()
        v = _vote(proposal=p, choice={"1": 2}, reason="why", is_override=True)
        assert Vote.from_db(p, v.to_db()) == v


# ---- API plumbing ---------------------------------------------------------------


class TestQueryApi:
    async def test_passes_configured_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_chain(monkeypatch, "hoodi")
        session = _ScriptedSession({"votes": []})
        monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: session)
        result = await signaling_module._query_api("proposal-votes", id="0xabc")
        assert result == {"votes": []}
        url, params = session.requests[0]
        assert url == "https://rocketdash.net/api/gov/proposal-votes"
        assert params == {"id": "0xabc", "network": "hoodi"}


class TestFetchProposals:
    async def test_newest_first(self, api: _ScriptedApi) -> None:
        api.proposals = [
            _proposal(proposal_id="0xold", start=1),
            _proposal(proposal_id="0xnew", start=2),
        ]
        proposals = await Signaling.fetch_proposals()
        assert [p.id for p in proposals] == ["0xnew", "0xold"]

    async def test_active_filters_closed(self, api: _ScriptedApi) -> None:
        api.proposals = [
            _proposal(proposal_id="0xopen"),
            _proposal(proposal_id="0xclosed", state="closed"),
        ]
        proposals = await Signaling.fetch_active_proposals()
        assert [p.id for p in proposals] == ["0xopen"]


class TestFetchDiscussionUrl:
    async def test_empty_url_is_none(self, api: _ScriptedApi) -> None:
        assert await Signaling.fetch_discussion_url("0xabc") is None

    async def test_returns_url(self, api: _ScriptedApi) -> None:
        api.discussion_url = "https://dao.rocketpool.net/t/1"
        url = await Signaling.fetch_discussion_url("0xabc")
        assert url == "https://dao.rocketpool.net/t/1"


class TestFetchVotes:
    async def test_attaches_proposal(self, api: _ScriptedApi) -> None:
        proposal = _proposal()
        api.votes[proposal.id] = [_vote_json(power=5.0)]
        votes = await Signaling.fetch_votes(proposal)
        assert len(votes) == 1
        assert votes[0].power == 5.0
        assert votes[0].proposal is proposal


# ---- Image rendering (render_to) ----------------------------------------------


class TestCreateImage:
    def test_renders_quorum_met_future(self) -> None:
        p = _proposal(scores=[60, 30, 10], quorum=100)
        assert p.create_image(include_title=True) is not None
        assert p.create_image(include_title=False) is not None

    def test_renders_quorum_unmet_past_many_choices(self) -> None:
        p = _proposal(
            choices=["A", "B", "C", "D", "E"],
            scores=[1, 1, 1, 1, 1],
            quorum=1000,
            end=1,
        )
        assert p.create_image(include_title=True) is not None


# ---- Proposal lifecycle events ------------------------------------------------


class TestProposalEvents:
    async def test_start_event(self, api: _ScriptedApi) -> None:
        ev = await _proposal().create_start_event()
        assert ev.event_name == "pdao_signaling_vote_start"
        assert ev.image is not None

    async def test_end_event_passed(self, api: _ScriptedApi) -> None:
        p = _proposal(choices=["For", "Against"], scores=[80, 10], quorum=50)
        ev = await p.create_end_event()
        assert ev.event_name == "pdao_signaling_vote_end"
        assert ev.embed.title is not None and "Passed" in ev.embed.title

    async def test_end_event_failed_when_against_wins(self, api: _ScriptedApi) -> None:
        p = _proposal(choices=["For", "Against"], scores=[10, 80], quorum=50)
        ev = await p.create_end_event()
        assert ev.embed.title is not None and "Failed" in ev.embed.title

    async def test_end_event_failed_without_quorum(self, api: _ScriptedApi) -> None:
        p = _proposal(choices=["For", "Against"], scores=[80, 10], quorum=500)
        ev = await p.create_end_event()
        assert ev.embed.title is not None and "Failed" in ev.embed.title

    def test_reached_quorum_event(self) -> None:
        ev = _proposal().create_reached_quorum_event(500)  # type: ignore[arg-type]
        assert ev.event_name == "pdao_signaling_vote_quorum"
        assert ev.block_number == 500


# ---- Vote → Event -------------------------------------------------------------


class TestVoteCreateEvent:
    async def test_new_high_power_vote_uses_image(self, api: _ScriptedApi) -> None:
        ev = await _vote(power=300.0).create_event(None)
        assert ev is not None
        assert ev.event_name == "pdao_signaling_vote"
        assert ev.image is not None

    async def test_low_power_vote_uses_thumbnail(self, api: _ScriptedApi) -> None:
        ev = await _vote(power=10.0).create_event(None)
        assert ev is not None
        assert ev.event_name == "signaling_vote"
        assert ev.thumbnail is not None

    async def test_voter_is_node_and_signer_is_field(self, api: _ScriptedApi) -> None:
        ev = await _vote().create_event(None)
        assert ev is not None
        assert (ev.embed.description or "").startswith(f"[{_NODE}] voted")
        signer_field = next(f for f in ev.embed.fields if f.name == "Signer")
        assert signer_field.value == f"[{_SIGNER}]"

    async def test_override_is_called_out(self, api: _ScriptedApi) -> None:
        ev = await _vote(is_override=True).create_event(None)
        assert ev is not None
        assert "overrode their delegate" in (ev.embed.description or "")

    async def test_unchanged_vote_returns_none(self, api: _ScriptedApi) -> None:
        prev = _vote(choice=1, reason="x")
        new = _vote(choice=1, reason="x", timestamp=1_700_000_600)
        assert await new.create_event(prev) is None

    async def test_changed_choice_describes_switch(self, api: _ScriptedApi) -> None:
        prev = _vote(choice=1)
        ev = await _vote(choice=2, timestamp=1_700_000_600).create_event(prev)
        assert ev is not None
        assert "changed their vote" in (ev.embed.description or "")

    async def test_changed_reason_describes_reason(self, api: _ScriptedApi) -> None:
        prev = _vote(reason="old")
        ev = await _vote(reason="new", timestamp=1_700_000_600).create_event(prev)
        assert ev is not None
        assert "changed the reason" in (ev.embed.description or "")

    async def test_added_reason_names_voter(self, api: _ScriptedApi) -> None:
        prev = _vote(reason="")
        ev = await _vote(reason="new", timestamp=1_700_000_600).create_event(prev)
        assert ev is not None
        assert (ev.embed.description or "").startswith(
            f"[{_NODE}] added context to their vote"
        )

    async def test_overlong_reason_is_truncated(self, api: _ScriptedApi) -> None:
        ev = await _vote(reason="x" * 2100).create_event(None)
        assert ev is not None
        assert (ev.embed.description or "").endswith("...```")


# ---- _get_new_events ----------------------------------------------------------


class TestGetNewEvents:
    async def test_new_proposal_emits_start_and_persists(
        self, mongo_db: AsyncDatabase[dict[str, Any]], api: _ScriptedApi
    ) -> None:
        api.proposals = [_proposal(proposal_id="0xnew")]

        cog = Signaling(make_bot(db=mongo_db))
        events = await cog._get_new_events()

        assert [e.event_name for e in events] == ["pdao_signaling_vote_start"]
        assert await mongo_db.signaling_proposals.find_one({"_id": "0xnew"})

    async def test_closed_proposal_is_not_announced(
        self, mongo_db: AsyncDatabase[dict[str, Any]], api: _ScriptedApi
    ) -> None:
        api.proposals = [_proposal(proposal_id="0xdone", state="closed")]

        cog = Signaling(make_bot(db=mongo_db))
        assert await cog._get_new_events() == []

    async def test_votes_before_discovery_are_recorded_silently(
        self, mongo_db: AsyncDatabase[dict[str, Any]], api: _ScriptedApi
    ) -> None:
        api.proposals = [_proposal(proposal_id="0xnew")]
        api.votes["0xnew"] = [_vote_json(power=300.0)]

        cog = Signaling(make_bot(db=mongo_db))
        events = await cog._get_new_events()

        assert [e.event_name for e in events] == ["pdao_signaling_vote_start"]
        assert await mongo_db.signaling_votes.find_one({"proposal_id": "0xnew"})
        assert await cog._get_new_events() == []

    async def test_ended_proposal_emits_end_and_cleans_up(
        self, mongo_db: AsyncDatabase[dict[str, Any]], api: _ScriptedApi
    ) -> None:
        await mongo_db.signaling_proposals.insert_one({"_id": "0xold", "quorum": True})
        await mongo_db.signaling_votes.insert_one({"_id": "x", "proposal_id": "0xold"})
        api.proposals = [_proposal(proposal_id="0xold", state="closed")]

        cog = Signaling(make_bot(db=mongo_db))
        events = await cog._get_new_events()

        assert [e.event_name for e in events] == ["pdao_signaling_vote_end"]
        assert await mongo_db.signaling_proposals.find_one({"_id": "0xold"}) is None
        assert await mongo_db.signaling_votes.find_one({"proposal_id": "0xold"}) is None

    async def test_deleted_proposal_is_dropped_without_event(
        self, mongo_db: AsyncDatabase[dict[str, Any]], api: _ScriptedApi
    ) -> None:
        await mongo_db.signaling_proposals.insert_one(
            {"_id": "0xgone", "quorum": False}
        )

        cog = Signaling(make_bot(db=mongo_db))
        assert await cog._get_new_events() == []
        assert await mongo_db.signaling_proposals.find_one({"_id": "0xgone"}) is None

    async def test_new_vote_emits_event_once(
        self, mongo_db: AsyncDatabase[dict[str, Any]], api: _ScriptedApi
    ) -> None:
        await mongo_db.signaling_proposals.insert_one(
            {"_id": "0xactive", "quorum": True}
        )
        api.proposals = [_proposal(proposal_id="0xactive")]
        api.votes["0xactive"] = [_vote_json(power=300.0)]

        cog = Signaling(make_bot(db=mongo_db))
        events = await cog._get_new_events()

        assert [e.event_name for e in events] == ["pdao_signaling_vote"]
        assert await cog._get_new_events() == []

    async def test_changed_vote_emits_change_event(
        self, mongo_db: AsyncDatabase[dict[str, Any]], api: _ScriptedApi
    ) -> None:
        await mongo_db.signaling_proposals.insert_one(
            {"_id": "0xactive", "quorum": True}
        )
        api.proposals = [_proposal(proposal_id="0xactive")]
        api.votes["0xactive"] = [_vote_json(choice=1, timestamp=1)]
        cog = Signaling(make_bot(db=mongo_db))
        await cog._get_new_events()

        api.votes["0xactive"] = [_vote_json(choice=2, timestamp=2)]
        events = await cog._get_new_events()

        assert len(events) == 1
        assert "changed their vote" in (events[0].embed.description or "")

    async def test_known_proposal_reaching_quorum_emits_event(
        self, mongo_db: AsyncDatabase[dict[str, Any]], api: _ScriptedApi
    ) -> None:
        await mongo_db.signaling_proposals.insert_one({"_id": "0xq", "quorum": False})
        api.proposals = [_proposal(proposal_id="0xq", scores=[60, 30, 10], quorum=100)]

        cog = Signaling(make_bot(db=mongo_db))
        events = await cog._get_new_events()

        assert [e.event_name for e in events] == ["pdao_signaling_vote_quorum"]
        doc = await mongo_db.signaling_proposals.find_one({"_id": "0xq"})
        assert doc is not None and doc["quorum"] is True


# ---- signaling_votes command --------------------------------------------------


class TestSignalingVotesCommand:
    async def test_no_active_proposals_returns_no_proposals_message(
        self, api: _ScriptedApi
    ) -> None:
        cog = Signaling(make_bot())
        interaction = make_interaction()
        await run_command(cog, "signaling_votes", interaction)
        embed = captured_embed(interaction)
        assert embed.description == "No active proposals."
        assert "file" not in interaction.followup.send.call_args.kwargs

    async def test_renders_grid_for_active_proposals(self, api: _ScriptedApi) -> None:
        api.proposals = [_proposal()]
        cog = Signaling(make_bot())
        interaction = make_interaction()
        await run_command(cog, "signaling_votes", interaction)
        embed = captured_embed(interaction)
        assert embed.image.url is not None
        assert embed.image.url.startswith("attachment://")
        assert "file" in interaction.followup.send.call_args.kwargs
