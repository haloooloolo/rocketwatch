from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from rocketwatch.utils.dao import (
    DAO,
    DefaultDAO,
    OracleDAO,
    ProtocolDAO,
    SecurityCouncil,
    _share_repr,
    build_claimer_description,
    decode_setting_multi,
    wrap_member_address,
)
from tests.lib.scripted_rocketpool import ScriptedRocketPool


class TestSanitize:
    def test_passes_through_short_messages_unchanged(self):
        msg = "Concise proposal"
        assert DAO.sanitize(msg) == msg

    def test_at_max_length_unchanged(self):
        msg = "a" * 150
        assert DAO.sanitize(msg) == msg

    def test_over_max_length_truncates_with_ellipsis(self):
        msg = "a" * 200
        out = DAO.sanitize(msg)
        # 150 chars total, last char is the ellipsis, preceded by 149 'a's.
        assert len(out) == 150
        assert out.endswith("…")
        assert out[:-1] == "a" * 149


class TestShareRepr:
    def test_zero_yields_no_asterisks(self):
        assert _share_repr(0) == ""

    def test_full_yields_max_width_asterisks(self):
        # 100% with default max_width=35.
        assert _share_repr(100) == "*" * 35

    def test_proportional_count(self):
        # 50% of 35 = 17.5 → rounds to 18 (banker's rounding goes to even).
        out = _share_repr(50)
        assert len(out) in (17, 18)
        # And the only allowed character is '*'.
        assert set(out) == {"*"}

    def test_custom_max_width(self):
        assert _share_repr(100, max_width=10) == "*" * 10


class TestBuildClaimerDescription:
    def test_renders_three_share_lines(self):
        # Args store shares as 10**16-denominated integers (50% → 50 * 10**16).
        out = build_claimer_description(
            {
                "nodePercent": 50 * 10**16,
                "protocolPercent": 30 * 10**16,
                "trustedNodePercent": 20 * 10**16,
            }
        )
        assert "Node Operator Share" in out
        assert "Protocol DAO Share" in out
        assert "Oracle DAO Share" in out
        # Percentages formatted as 1-decimal floats.
        assert "50.0%" in out
        assert "30.0%" in out
        assert "20.0%" in out


class TestDecodeSettingMulti:
    def test_decodes_uint_bool_and_address(self, monkeypatch: pytest.MonkeyPatch):
        # The function dispatches on a parallel `types` array:
        #   0=uint256, 1=bool, 2=address. Stub `w3.to_int` and
        #   `w3.to_checksum_address` to verify the dispatch.
        from rocketwatch.utils import dao as dao_module

        monkeypatch.setattr(dao_module.w3, "to_int", lambda b: int.from_bytes(b))
        monkeypatch.setattr(
            dao_module.w3, "to_checksum_address", lambda b: "0xchecksum"
        )

        args: Mapping[str, Any] = {
            "settingContractNames": ["A", "B", "C"],
            "settingPaths": ["a.x", "b.y", "c.z"],
            "types": [0, 1, 2],
        }
        values = [b"\x00\x05", b"\x01", b"\x00" * 20]
        out = decode_setting_multi(args, values)
        # uint => decoded as 5 from b'\x00\x05'.
        assert "`a.x` set to `5`" in out
        # bool => any non-empty bytes is truthy.
        assert "`b.y` set to `True`" in out
        # address => routed through to_checksum_address stub.
        assert "`c.z` set to `0xchecksum`" in out

    def test_unknown_type_falls_through_to_placeholder(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from rocketwatch.utils import dao as dao_module

        monkeypatch.setattr(dao_module.w3, "to_int", lambda b: 0)
        args: Mapping[str, Any] = {
            "settingContractNames": ["A"],
            "settingPaths": ["a.x"],
            "types": [99],
        }
        out = decode_setting_multi(args, [b""])
        assert "`a.x` set to `???`" in out


class TestDefaultDaoInit:
    def test_odao_display_name(self):
        dao = DefaultDAO("rocketDAONodeTrustedProposals")
        assert dao.display_name == "oDAO"
        assert dao.contract_name == "rocketDAONodeTrustedProposals"

    def test_security_council_display_name(self):
        dao = DefaultDAO("rocketDAOSecurityProposals")
        assert dao.display_name == "Security Council"

    def test_unknown_dao_raises(self):
        with pytest.raises(ValueError, match="Unknown DAO"):
            DefaultDAO("rocketRandomThing")  # type: ignore[arg-type]


class TestSubclassDefaults:
    def test_oracle_dao_inherits_odao_setup(self):
        dao = OracleDAO()
        assert dao.contract_name == "rocketDAONodeTrustedProposals"
        assert dao.display_name == "oDAO"

    def test_security_council_inherits_setup(self):
        dao = SecurityCouncil()
        assert dao.contract_name == "rocketDAOSecurityProposals"
        assert dao.display_name == "Security Council"


class TestProtocolDaoProposalVotesTotal:
    def test_excludes_veto_includes_abstain(self):
        # The `votes_total` property sums for+against+abstain but NOT veto —
        # that's how quorum math works for the pDAO.
        p = ProtocolDAO.Proposal(
            id=1,
            proposer="0x0",  # type: ignore[arg-type]
            message="m",
            payload=b"",
            created=0,
            start=0,
            end_phase_1=0,
            end_phase_2=0,
            expires=0,
            votes_for=100.0,
            votes_against=50.0,
            votes_veto=999.0,
            votes_abstain=10.0,
            quorum=200.0,
            veto_quorum=1000.0,
        )
        assert p.votes_total == 160.0


def _default_proposal(**overrides: Any) -> DefaultDAO.Proposal:
    base = {
        "id": 1,
        "proposer": "0xPROPOSER",
        "message": "do the thing",
        "payload": b"",
        "created": 0,
        "start": 0,
        "end": 0,
        "expires": 0,
        "votes_for": 10,
        "votes_against": 5,
        "votes_required": 8.0,
    }
    base.update(overrides)
    return DefaultDAO.Proposal(**base)  # type: ignore[arg-type]


def _protocol_proposal(**overrides: Any) -> ProtocolDAO.Proposal:
    base = {
        "id": 1,
        "proposer": "0xPROPOSER",
        "message": "do the thing",
        "payload": b"",
        "created": 0,
        "start": 0,
        "end_phase_1": 0,
        "end_phase_2": 0,
        "expires": 0,
        "votes_for": 100.0,
        "votes_against": 50.0,
        "votes_veto": 0.0,
        "votes_abstain": 25.0,
        "quorum": 150.0,
        "veto_quorum": 200.0,
    }
    base.update(overrides)
    return ProtocolDAO.Proposal(**base)  # type: ignore[arg-type]


class TestDefaultDaoBuildVoteGraph:
    def test_renders_quorum_line_when_quorum_met(self) -> None:
        dao = DefaultDAO("rocketDAONodeTrustedProposals")
        out = dao._build_vote_graph(_default_proposal(votes_for=10, votes_required=8.0))
        assert "Quorum:" in out
        assert "✔" in out  # quorum_perc >= 1 → checkmark

    def test_omits_checkmark_when_quorum_not_met(self) -> None:
        dao = DefaultDAO("rocketDAONodeTrustedProposals")
        out = dao._build_vote_graph(
            _default_proposal(votes_for=2, votes_against=1, votes_required=10.0)
        )
        assert "Quorum:" in out
        assert "✔" not in out


class TestProtocolDaoBuildVoteGraph:
    def test_renders_main_quorum_line(self) -> None:
        dao = ProtocolDAO()
        out = dao._build_vote_graph(_protocol_proposal())
        assert "Quorum:" in out
        # No veto block when votes_veto == 0.
        assert "Veto" not in out

    def test_appends_veto_section_when_veto_nonzero(self) -> None:
        dao = ProtocolDAO()
        out = dao._build_vote_graph(
            _protocol_proposal(votes_veto=50.0, veto_quorum=40.0)
        )
        assert "Veto" in out
        # Second quorum (veto) is met → second checkmark present.
        assert out.count("✔") >= 1


class TestDefaultDaoFetchProposal:
    async def test_returns_decoded_proposal(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        # rp.multicall walks the list in order — script each call by name+args
        # so they're returned in the order the implementation asks.
        scripted_rp.set_call("rocketDAOProposal.getProposer", lambda pid: "0xPROPOSER")
        scripted_rp.set_call("rocketDAOProposal.getMessage", lambda pid: "go go go")
        scripted_rp.set_call("rocketDAOProposal.getPayload", lambda pid: b"\x01\x02")
        scripted_rp.set_call("rocketDAOProposal.getCreated", lambda pid: 100)
        scripted_rp.set_call("rocketDAOProposal.getStart", lambda pid: 110)
        scripted_rp.set_call("rocketDAOProposal.getEnd", lambda pid: 120)
        scripted_rp.set_call("rocketDAOProposal.getExpires", lambda pid: 130)
        scripted_rp.set_call("rocketDAOProposal.getVotesFor", lambda pid: 1 * 10**18)
        scripted_rp.set_call(
            "rocketDAOProposal.getVotesAgainst", lambda pid: 2 * 10**17
        )
        scripted_rp.set_call(
            "rocketDAOProposal.getVotesRequired", lambda pid: 5 * 10**17
        )

        dao = DefaultDAO("rocketDAONodeTrustedProposals")
        proposal = await dao.fetch_proposal(42)
        assert proposal.id == 42
        assert proposal.message == "go go go"
        assert proposal.payload == b"\x01\x02"
        assert proposal.start == 110
        # votes_for is an int (to_int) — 1 ETH worth → 1
        assert proposal.votes_for == 1
        assert proposal.votes_required == pytest.approx(0.5)


class TestDefaultDaoGetProposalIdsByState:
    async def test_groups_by_state(self, scripted_rp: ScriptedRocketPool) -> None:
        # 3 proposals total; only ids that belong to *this* DAO contract are
        # filtered in. (getDAO returns the DAO name per proposal.)
        dao_names_by_id = {
            1: "rocketDAONodeTrustedProposals",
            2: "rocketDAOSecurityProposals",  # different DAO — filtered out
            3: "rocketDAONodeTrustedProposals",
        }
        states_by_id = {
            1: DefaultDAO.ProposalState.Active,
            3: DefaultDAO.ProposalState.Executed,
        }

        scripted_rp.set_call("rocketDAOProposal.getTotal", 3)
        scripted_rp.set_call(
            "rocketDAOProposal.getDAO",
            lambda pid: dao_names_by_id[pid],
        )
        scripted_rp.set_call(
            "rocketDAOProposal.getState",
            lambda pid: states_by_id[pid],
        )

        dao = DefaultDAO("rocketDAONodeTrustedProposals")
        out = await dao.get_proposal_ids_by_state()
        assert out[DefaultDAO.ProposalState.Active] == [1]
        assert out[DefaultDAO.ProposalState.Executed] == [3]
        # Proposal 2 was for a different DAO and never enters the buckets.
        for ids in out.values():
            assert 2 not in ids


class TestProtocolDaoGetProposalIdsByState:
    async def test_buckets_all_ids(self, scripted_rp: ScriptedRocketPool) -> None:
        states = {
            1: ProtocolDAO.ProposalState.ActivePhase1,
            2: ProtocolDAO.ProposalState.Executed,
            3: ProtocolDAO.ProposalState.Vetoed,
        }
        scripted_rp.set_call("rocketDAOProtocolProposal.getTotal", 3)
        scripted_rp.set_call(
            "rocketDAOProtocolProposal.getState", lambda pid: states[pid]
        )

        out = await ProtocolDAO().get_proposal_ids_by_state()
        assert out[ProtocolDAO.ProposalState.ActivePhase1] == [1]
        assert out[ProtocolDAO.ProposalState.Executed] == [2]
        assert out[ProtocolDAO.ProposalState.Vetoed] == [3]


class TestProtocolDaoFetchProposal:
    async def test_returns_decoded_proposal(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        scripted_rp.set_call(
            "rocketDAOProtocolProposal.getProposer", lambda pid: "0xPROPOSER"
        )
        scripted_rp.set_call(
            "rocketDAOProtocolProposal.getMessage", lambda pid: "phase 1"
        )
        scripted_rp.set_call(
            "rocketDAOProtocolProposal.getPayload", lambda pid: b"\xaa"
        )
        scripted_rp.set_call("rocketDAOProtocolProposal.getCreated", lambda pid: 1)
        scripted_rp.set_call("rocketDAOProtocolProposal.getStart", lambda pid: 2)
        scripted_rp.set_call("rocketDAOProtocolProposal.getPhase1End", lambda pid: 3)
        scripted_rp.set_call("rocketDAOProtocolProposal.getPhase2End", lambda pid: 4)
        scripted_rp.set_call("rocketDAOProtocolProposal.getExpires", lambda pid: 5)
        scripted_rp.set_call(
            "rocketDAOProtocolProposal.getVotingPowerFor", lambda pid: 1 * 10**18
        )
        scripted_rp.set_call(
            "rocketDAOProtocolProposal.getVotingPowerAgainst",
            lambda pid: 2 * 10**17,
        )
        scripted_rp.set_call(
            "rocketDAOProtocolProposal.getVotingPowerVeto", lambda pid: 0
        )
        scripted_rp.set_call(
            "rocketDAOProtocolProposal.getVotingPowerAbstained",
            lambda pid: 3 * 10**17,
        )
        scripted_rp.set_call(
            "rocketDAOProtocolProposal.getVotingPowerRequired", lambda pid: 10**18
        )
        scripted_rp.set_call(
            "rocketDAOProtocolProposal.getVetoQuorum", lambda pid: 5 * 10**17
        )

        proposal = await ProtocolDAO().fetch_proposal(7)
        assert proposal.id == 7
        assert proposal.message == "phase 1"
        assert proposal.end_phase_1 == 3
        assert proposal.end_phase_2 == 4
        assert proposal.votes_for == pytest.approx(1.0)
        assert proposal.votes_abstain == pytest.approx(0.3)
        assert proposal.veto_quorum == pytest.approx(0.5)


class TestBuildProposalBody:
    async def test_decodes_payload_via_contract(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The decode path calls contract.decode_function_input(payload).
        # ScriptedRocketPool's contract stub doesn't model decode, so swap in
        # a contract with a deterministic decoder.
        function_obj = MagicMock()
        function_obj.abi_element_identifier = "setSetting(uint256)"
        decoded_contract = MagicMock()
        decoded_contract.decode_function_input.return_value = (
            function_obj,
            {"value": 42},
        )

        async def fake_get_contract(_self: Any) -> Any:
            return decoded_contract

        monkeypatch.setattr(
            "rocketwatch.utils.dao.DAO._get_contract", fake_get_contract
        )

        dao = DefaultDAO("rocketDAONodeTrustedProposals")
        body = await dao.build_proposal_body(
            _default_proposal(payload=b"\x01\x02"),
            include_proposer=True,
            include_payload=True,
            include_votes=False,
        )
        assert "Description:" in body
        assert "Proposed by:" in body
        assert "setSetting" in body
        assert "value = 42" in body

    async def test_falls_back_to_raw_hex_when_decode_fails(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad_contract = MagicMock()
        bad_contract.decode_function_input.side_effect = ValueError("bad payload")

        async def fake_get_contract(_self: Any) -> Any:
            return bad_contract

        monkeypatch.setattr(
            "rocketwatch.utils.dao.DAO._get_contract", fake_get_contract
        )

        dao = DefaultDAO("rocketDAONodeTrustedProposals")
        body = await dao.build_proposal_body(
            _default_proposal(payload=b"\xde\xad\xbe\xef"),
            include_proposer=False,
            include_payload=True,
            include_votes=False,
        )
        assert "failed to decode" in body
        assert "deadbeef" in body

    async def test_omits_sections_when_flagged_off(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        dao = DefaultDAO("rocketDAONodeTrustedProposals")
        body = await dao.build_proposal_body(
            _default_proposal(message="hello"),
            include_proposer=False,
            include_payload=False,
            include_votes=False,
        )
        assert body == "Description:\nhello"


class TestContractCaching:
    async def test_contract_resolved_once_then_cached(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Track how many times rp.get_contract_by_name is invoked.
        calls: list[str] = []
        sentinel = MagicMock(name="contract")

        async def counting_get(name: str, mainnet: bool = False) -> Any:
            calls.append(name)
            return sentinel

        monkeypatch.setattr(scripted_rp, "get_contract_by_name", counting_get)

        dao = DefaultDAO("rocketDAONodeTrustedProposals")
        c1 = await dao._get_contract()
        c2 = await dao._get_contract()
        assert c1 is sentinel
        assert c2 is sentinel
        # Cached: only one underlying resolution.
        assert calls == ["rocketDAONodeTrustedProposals"]

    async def test_proposal_contract_resolved_once_then_cached(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        sentinel = MagicMock(name="proposal_contract")

        async def counting_get(name: str, mainnet: bool = False) -> Any:
            calls.append(name)
            return sentinel

        monkeypatch.setattr(scripted_rp, "get_contract_by_name", counting_get)

        dao = DefaultDAO("rocketDAONodeTrustedProposals")
        await dao._get_proposal_contract()
        await dao._get_proposal_contract()
        assert calls == ["rocketDAOProposal"]


class TestWrapMemberAddress:
    async def test_delegates_to_el_explorer_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = AsyncMock(return_value="link")
        monkeypatch.setattr("rocketwatch.utils.dao.el_explorer_url", fake)
        out = await wrap_member_address("0xABC", 100)  # type: ignore[arg-type]
        assert out == "link"
        fake.assert_awaited_once_with("0xABC", block=100)
