from unittest.mock import AsyncMock, MagicMock

import pytest

from rocketwatch.plugins.dao.dao import OnchainDAO
from rocketwatch.utils.dao import DefaultDAO, ProtocolDAO
from tests.lib.discord_harness import (
    make_bot,
    make_interaction,
    run_command,
)


def _odao_proposal(
    *,
    proposal_id: int = 1,
    message: str = "Add new member",
    proposer: str = "0x" + "11" * 20,
) -> DefaultDAO.Proposal:
    return DefaultDAO.Proposal(
        id=proposal_id,
        proposer=proposer,  # type: ignore[arg-type]
        message=message,
        payload=b"",
        created=1_700_000_000,
        start=1_700_001_000,
        end=1_700_002_000,
        expires=1_700_003_000,
        votes_for=2,
        votes_against=1,
        votes_required=2.0,
    )


def _pdao_proposal(
    *, proposal_id: int = 1, message: str = "Update protocol fee"
) -> ProtocolDAO.Proposal:
    return ProtocolDAO.Proposal(
        id=proposal_id,
        proposer="0x" + "00" * 20,  # type: ignore[arg-type]
        message=message,
        payload=b"",
        created=1_700_000_000,
        start=1_700_001_000,
        end_phase_1=1_700_002_000,
        end_phase_2=1_700_003_000,
        expires=1_700_004_000,
        votes_for=10.0,
        votes_against=5.0,
        votes_veto=0.0,
        votes_abstain=1.0,
        quorum=10.0,
        veto_quorum=20.0,
    )


def _fake_dao(
    *,
    display_name: str,
    state_to_proposals: dict[int, list[DefaultDAO.Proposal | ProtocolDAO.Proposal]],
    proposal_states: type,
) -> MagicMock:
    """Build a stand-in for an instantiated DAO with the methods this plugin uses.

    `state_to_proposals` maps `ProposalState` int values to the proposals that
    should be returned for that state."""
    dao = MagicMock()
    dao.display_name = display_name
    dao.ProposalState = proposal_states

    # `get_proposal_ids_by_state` returns {state: [ids]}; the plugin then
    # calls `fetch_proposal(id)` to materialise each one. Build a lookup
    # of id → proposal so fetch_proposal stays consistent.
    ids_by_state: dict[int, list[int]] = {
        state_value: [p.id for p in proposals]
        for state_value, proposals in state_to_proposals.items()
    }
    by_id: dict[int, object] = {}
    for proposals in state_to_proposals.values():
        for p in proposals:
            by_id[p.id] = p

    dao.get_proposal_ids_by_state = AsyncMock(return_value=ids_by_state)
    dao.fetch_proposal = AsyncMock(side_effect=lambda pid: by_id[pid])
    dao.build_proposal_body = AsyncMock(return_value="...body...")
    return dao


# ---- get_dao_votes_embed (oDAO / Security Council) ----------------------------


class TestGetDaoVotesEmbed:
    async def test_no_proposals_renders_empty_message(self) -> None:
        dao = _fake_dao(
            display_name="oDAO",
            state_to_proposals={
                DefaultDAO.ProposalState.Pending: [],
                DefaultDAO.ProposalState.Active: [],
                DefaultDAO.ProposalState.Succeeded: [],
            },
            proposal_states=DefaultDAO.ProposalState,
        )
        embed = await OnchainDAO.get_dao_votes_embed(dao, full=False)
        assert embed.title == "oDAO Proposals"
        assert embed.description == "No active proposals."

    async def test_pending_proposal_renders_voting_start_window(self) -> None:
        dao = _fake_dao(
            display_name="oDAO",
            state_to_proposals={
                DefaultDAO.ProposalState.Pending: [_odao_proposal(proposal_id=1)],
                DefaultDAO.ProposalState.Active: [],
                DefaultDAO.ProposalState.Succeeded: [],
            },
            proposal_states=DefaultDAO.ProposalState,
        )
        embed = await OnchainDAO.get_dao_votes_embed(dao, full=False)
        desc = embed.description or ""
        assert "Proposal #1" in desc
        assert "Pending" in desc
        # The body block is followed by start/end markers.
        assert "Voting starts" in desc

    async def test_active_proposal_includes_votes_in_body(self) -> None:
        dao = _fake_dao(
            display_name="oDAO",
            state_to_proposals={
                DefaultDAO.ProposalState.Pending: [],
                DefaultDAO.ProposalState.Active: [_odao_proposal(proposal_id=2)],
                DefaultDAO.ProposalState.Succeeded: [],
            },
            proposal_states=DefaultDAO.ProposalState,
        )
        await OnchainDAO.get_dao_votes_embed(dao, full=False)
        # The Active branch always calls build_proposal_body with
        # include_votes=True (regardless of `full`).
        active_call = next(
            c
            for c in dao.build_proposal_body.await_args_list
            if c.kwargs.get("include_votes") is True
        )
        assert active_call.kwargs["include_votes"] is True

    async def test_succeeded_proposal_uses_full_flag_for_votes(self) -> None:
        # For Succeeded proposals, include_votes mirrors the `full` flag —
        # not a hardcoded True.
        dao = _fake_dao(
            display_name="oDAO",
            state_to_proposals={
                DefaultDAO.ProposalState.Pending: [],
                DefaultDAO.ProposalState.Active: [],
                DefaultDAO.ProposalState.Succeeded: [_odao_proposal(proposal_id=9)],
            },
            proposal_states=DefaultDAO.ProposalState,
        )
        await OnchainDAO.get_dao_votes_embed(dao, full=False)
        # Single call → must have include_votes=False because full=False.
        call = dao.build_proposal_body.await_args_list[0]
        assert call.kwargs["include_votes"] is False

    async def test_succeeded_with_full_includes_votes(self) -> None:
        dao = _fake_dao(
            display_name="oDAO",
            state_to_proposals={
                DefaultDAO.ProposalState.Pending: [],
                DefaultDAO.ProposalState.Active: [],
                DefaultDAO.ProposalState.Succeeded: [_odao_proposal(proposal_id=9)],
            },
            proposal_states=DefaultDAO.ProposalState,
        )
        await OnchainDAO.get_dao_votes_embed(dao, full=True)
        call = dao.build_proposal_body.await_args_list[0]
        assert call.kwargs["include_votes"] is True


# ---- get_pdao_votes_embed -----------------------------------------------------


class TestGetPdaoVotesEmbed:
    async def test_no_proposals_renders_empty_message(self) -> None:
        dao = _fake_dao(
            display_name="pDAO",
            state_to_proposals={
                ProtocolDAO.ProposalState.Pending: [],
                ProtocolDAO.ProposalState.ActivePhase1: [],
                ProtocolDAO.ProposalState.ActivePhase2: [],
                ProtocolDAO.ProposalState.Succeeded: [],
            },
            proposal_states=ProtocolDAO.ProposalState,
        )
        embed = await OnchainDAO.get_pdao_votes_embed(dao, full=False)
        assert embed.title == "pDAO Proposals"
        assert embed.description == "No active proposals."

    async def test_phase1_and_phase2_each_render(self) -> None:
        dao = _fake_dao(
            display_name="pDAO",
            state_to_proposals={
                ProtocolDAO.ProposalState.Pending: [],
                ProtocolDAO.ProposalState.ActivePhase1: [
                    _pdao_proposal(proposal_id=11)
                ],
                ProtocolDAO.ProposalState.ActivePhase2: [
                    _pdao_proposal(proposal_id=22)
                ],
                ProtocolDAO.ProposalState.Succeeded: [],
            },
            proposal_states=ProtocolDAO.ProposalState,
        )
        embed = await OnchainDAO.get_pdao_votes_embed(dao, full=False)
        desc = embed.description or ""
        assert "Proposal #11" in desc
        assert "Phase 1" in desc
        assert "Proposal #22" in desc
        assert "Phase 2" in desc

    async def test_pending_render_uses_phase2_for_end_window(self) -> None:
        # The "voting starts ... ends" pair for Pending proposals uses
        # end_phase_2 as the end timestamp.
        proposal = _pdao_proposal(proposal_id=7)
        dao = _fake_dao(
            display_name="pDAO",
            state_to_proposals={
                ProtocolDAO.ProposalState.Pending: [proposal],
                ProtocolDAO.ProposalState.ActivePhase1: [],
                ProtocolDAO.ProposalState.ActivePhase2: [],
                ProtocolDAO.ProposalState.Succeeded: [],
            },
            proposal_states=ProtocolDAO.ProposalState,
        )
        embed = await OnchainDAO.get_pdao_votes_embed(dao, full=False)
        desc = embed.description or ""
        # `<t:end_phase_2:R>` appears in the description as the closing timestamp.
        assert f"<t:{proposal.end_phase_2}:R>" in desc


# ---- dao_votes command routing ------------------------------------------------


class TestDaoVotesCommand:
    async def test_pdao_route_calls_protocol_dao_embed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured_args: dict[str, object] = {}

        async def fake_pdao_embed(dao: object, full: bool) -> object:
            captured_args["dao"] = dao
            captured_args["full"] = full
            from rocketwatch.utils.embeds import Embed as E

            e = E()
            e.title = "pDAO Proposals"
            return e

        monkeypatch.setattr(
            OnchainDAO, "get_pdao_votes_embed", staticmethod(fake_pdao_embed)
        )
        # ProtocolDAO() factory must not touch the chain; stub it.
        monkeypatch.setattr(
            "rocketwatch.plugins.dao.dao.ProtocolDAO", lambda: "pdao-instance"
        )

        cog = OnchainDAO(make_bot())
        interaction = make_interaction()
        embed = await run_command(cog, "dao_votes", interaction, "pDAO", False)
        assert embed.title == "pDAO Proposals"
        assert captured_args == {"dao": "pdao-instance", "full": False}

    async def test_odao_route_calls_dao_votes_embed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured_args: dict[str, object] = {}

        async def fake_dao_embed(dao: object, full: bool) -> object:
            captured_args["dao"] = dao
            captured_args["full"] = full
            from rocketwatch.utils.embeds import Embed as E

            e = E()
            e.title = "oDAO Proposals"
            return e

        monkeypatch.setattr(
            OnchainDAO, "get_dao_votes_embed", staticmethod(fake_dao_embed)
        )
        monkeypatch.setattr(
            "rocketwatch.plugins.dao.dao.OracleDAO", lambda: "odao-instance"
        )

        cog = OnchainDAO(make_bot())
        embed = await run_command(cog, "dao_votes", make_interaction(), "oDAO", True)
        assert embed.title == "oDAO Proposals"
        assert captured_args == {"dao": "odao-instance", "full": True}

    async def test_security_council_route_calls_dao_votes_embed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured_args: dict[str, object] = {}

        async def fake_dao_embed(dao: object, full: bool) -> object:
            captured_args["dao"] = dao
            from rocketwatch.utils.embeds import Embed as E

            e = E()
            e.title = "Security Council Proposals"
            return e

        monkeypatch.setattr(
            OnchainDAO, "get_dao_votes_embed", staticmethod(fake_dao_embed)
        )
        monkeypatch.setattr(
            "rocketwatch.plugins.dao.dao.SecurityCouncil",
            lambda: "sc-instance",
        )

        cog = OnchainDAO(make_bot())
        await run_command(
            cog, "dao_votes", make_interaction(), "Security Council", False
        )
        assert captured_args["dao"] == "sc-instance"


# ---- Vote dataclass + VoterPageView title -------------------------------------


class TestVoteDataclass:
    def test_fields_are_assigned_positionally(self) -> None:
        v = OnchainDAO.Vote(
            "0xabc",  # type: ignore[arg-type]
            2,
            12.5,
            1_700_000_000,
        )
        assert v.voter == "0xabc"
        assert v.direction == 2
        assert v.voting_power == 12.5
        assert v.time == 1_700_000_000


class TestVoterPageViewTitle:
    def test_includes_proposal_id_in_title(self) -> None:
        view = OnchainDAO.VoterPageView(_pdao_proposal(proposal_id=42))
        assert view._title == "pDAO Proposal #42 - Voter List"


# ---- voter_list command: invalid proposal id ----------------------------------


class TestVoterListInvalidProposal:
    async def test_returns_plain_message_when_proposal_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stub ProtocolDAO() so the factory call doesn't touch the chain;
        # make .fetch_proposal return None to trigger the early-return branch.
        fake_dao = MagicMock()
        fake_dao.fetch_proposal = AsyncMock(return_value=None)
        monkeypatch.setattr("rocketwatch.plugins.dao.dao.ProtocolDAO", lambda: fake_dao)

        cog = OnchainDAO(make_bot())
        interaction = make_interaction()
        await cog.voter_list.callback(cog, interaction, 999)

        # Plain string send (no embed) for this edge case.
        interaction.followup.send.assert_awaited_once_with("Invalid proposal ID.")
