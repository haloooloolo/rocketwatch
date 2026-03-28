import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum
from typing import Literal, cast

import termplotlib as tpl
from eth_typing import ChecksumAddress

from utils import solidity
from utils.embeds import el_explorer_url
from utils.rocketpool import rp
from utils.shared_w3 import w3

log = logging.getLogger("rocketwatch.dao")


class DAO(ABC):
    def __init__(self, contract_name: str, proposal_contract_name: str):
        self.contract_name = contract_name
        self._proposal_contract_name = proposal_contract_name
        self._contract = None
        self._proposal_contract = None

    async def _get_contract(self):
        if self._contract is None:
            self._contract = await rp.get_contract_by_name(self.contract_name)
        return self._contract

    async def _get_proposal_contract(self):
        if self._proposal_contract is None:
            self._proposal_contract = await rp.get_contract_by_name(
                self._proposal_contract_name
            )
        return self._proposal_contract

    @dataclass(frozen=True, slots=True)
    class Proposal(ABC):
        id: int
        proposer: ChecksumAddress
        message: str
        payload: bytes
        created: int

    @abstractmethod
    async def fetch_proposal(self, proposal_id: int) -> Proposal:
        pass

    @abstractmethod
    def _build_vote_graph(self, proposal: Proposal) -> str:
        pass

    @staticmethod
    def sanitize(message: str) -> str:
        max_length = 150
        if len(message) > max_length:
            message = message[: (max_length - 1)] + "…"
        return message

    async def build_proposal_body(
        self,
        proposal: Proposal,
        *,
        include_proposer=True,
        include_payload=True,
        include_votes=True,
    ) -> str:
        body_repr = f"Description:\n{self.sanitize(proposal.message)}"

        if include_proposer:
            body_repr += f"\n\nProposed by:\n{proposal.proposer}"

        if include_payload:
            try:
                contract = await self._get_contract()
                decoded = contract.decode_function_input(proposal.payload)
                function = decoded[0].abi_element_identifier
                function_name = function.split("(")[0]
                args = [f"  {arg} = {value}" for arg, value in decoded[1].items()]
                payload_str = f"{function_name}(\n" + "\n".join(args) + "\n)"
                body_repr += f"\n\nPayload:\n{payload_str}"
            except Exception:
                # if this goes wrong, just use the raw payload
                log.exception("Failed to decode proposal payload")
                body_repr += (
                    f"\n\nRaw Payload (failed to decode):\n{proposal.payload.hex()}"
                )

        if include_votes:
            body_repr += f"\n\nVotes:\n{self._build_vote_graph(proposal)}"

        return body_repr


type DAOContractName = Literal[
    "rocketDAONodeTrustedProposals", "rocketDAOSecurityProposals"
]


class DefaultDAO(DAO):
    def __init__(
        self,
        contract_name: DAOContractName,
    ):
        if contract_name == "rocketDAONodeTrustedProposals":
            self.display_name = "oDAO"
        elif contract_name == "rocketDAOSecurityProposals":
            self.display_name = "Security Council"
        else:
            raise ValueError("Unknown DAO")
        super().__init__(contract_name, "rocketDAOProposal")

    class ProposalState(IntEnum):
        Pending = 0
        Active = 1
        Cancelled = 2
        Defeated = 3
        Succeeded = 4
        Expired = 5
        Executed = 6

    @dataclass(frozen=True, slots=True)
    class Proposal(DAO.Proposal):
        start: int
        end: int
        expires: int
        votes_for: int
        votes_against: int
        votes_required: int

    async def get_proposal_ids_by_state(self) -> dict[ProposalState, list[int]]:
        proposal_contract = await self._get_proposal_contract()
        num_proposals = await proposal_contract.functions.getTotal().call()
        proposal_dao_names = await rp.multicall(
            [
                proposal_contract.functions.getDAO(proposal_id)
                for proposal_id in range(1, num_proposals + 1)
            ]
        )

        relevant_proposals = [
            (i + 1)
            for (i, dao_name) in enumerate(proposal_dao_names)
            if (dao_name == self.contract_name)
        ]
        proposal_states = await rp.multicall(
            [
                proposal_contract.functions.getState(proposal_id)
                for proposal_id in relevant_proposals
            ]
        )

        proposals: dict[DefaultDAO.ProposalState, list[int]] = {
            state: [] for state in DefaultDAO.ProposalState
        }
        for proposal_id, state in zip(
            relevant_proposals, proposal_states, strict=False
        ):
            proposals[state].append(proposal_id)

        return proposals

    async def fetch_proposal(self, proposal_id: int) -> Proposal:
        proposal_contract = await self._get_proposal_contract()
        (
            proposer,
            message,
            payload,
            created,
            start,
            end,
            expires,
            votes_for_raw,
            votes_against_raw,
            votes_required_raw,
        ) = await rp.multicall(
            [
                proposal_contract.functions.getProposer(proposal_id),
                proposal_contract.functions.getMessage(proposal_id),
                proposal_contract.functions.getPayload(proposal_id),
                proposal_contract.functions.getCreated(proposal_id),
                proposal_contract.functions.getStart(proposal_id),
                proposal_contract.functions.getEnd(proposal_id),
                proposal_contract.functions.getExpires(proposal_id),
                proposal_contract.functions.getVotesFor(proposal_id),
                proposal_contract.functions.getVotesAgainst(proposal_id),
                proposal_contract.functions.getVotesRequired(proposal_id),
            ]
        )
        return DefaultDAO.Proposal(
            id=proposal_id,
            proposer=cast(ChecksumAddress, proposer),
            message=message,
            payload=payload,
            created=created,
            start=start,
            end=end,
            expires=expires,
            votes_for=solidity.to_int(votes_for_raw),
            votes_against=solidity.to_int(votes_against_raw),
            votes_required=solidity.to_float(votes_required_raw),
        )

    def _build_vote_graph(self, proposal: DAO.Proposal) -> str:
        assert isinstance(proposal, DefaultDAO.Proposal)
        votes_for = proposal.votes_for
        votes_against = proposal.votes_against
        votes_required = math.ceil(proposal.votes_required)

        graph = tpl.figure()
        graph.barh(
            [votes_for, votes_against, max([votes_for, votes_against, votes_required])],
            ["For", "Against", ""],
            max_width=12,
        )
        graph_bars = graph.get_string().split("\n")
        quorum_perc = max(votes_for, votes_against) / votes_required
        return (
            f"{graph_bars[0]: <{len(graph_bars[2])}}{'▏' if votes_for >= votes_against else ''}\n"
            f"{graph_bars[1]: <{len(graph_bars[2])}}{'▏' if votes_for <= votes_against else ''}\n"
            f"Quorum: {quorum_perc:.0%}{' ✔' if (quorum_perc >= 1) else ''}"
        )


class OracleDAO(DefaultDAO):
    def __init__(self):
        super().__init__("rocketDAONodeTrustedProposals")


class SecurityCouncil(DefaultDAO):
    def __init__(self):
        super().__init__("rocketDAOSecurityProposals")


class ProtocolDAO(DAO):
    def __init__(self):
        super().__init__("rocketDAOProtocolProposals", "rocketDAOProtocolProposal")

    class ProposalState(IntEnum):
        Pending = 0
        ActivePhase1 = 1
        ActivePhase2 = 2
        Destroyed = 3
        Vetoed = 4
        QuorumNotMet = 5
        Defeated = 6
        Succeeded = 7
        Expired = 8
        Executed = 9

    @dataclass(frozen=True, slots=True)
    class Proposal(DAO.Proposal):
        start: int
        end_phase_1: int
        end_phase_2: int
        expires: int
        votes_for: float
        votes_against: float
        votes_veto: float
        votes_abstain: float
        quorum: float
        veto_quorum: float

        @property
        def votes_total(self):
            return self.votes_for + self.votes_against + self.votes_abstain

    async def get_proposal_ids_by_state(self) -> dict[ProposalState, list[int]]:
        proposal_contract = await self._get_proposal_contract()
        num_proposals = await proposal_contract.functions.getTotal().call()
        proposal_states = await rp.multicall(
            [
                proposal_contract.functions.getState(proposal_id)
                for proposal_id in range(1, num_proposals + 1)
            ]
        )

        proposals: dict[ProtocolDAO.ProposalState, list[int]] = {
            state: [] for state in ProtocolDAO.ProposalState
        }
        for proposal_id in range(1, num_proposals + 1):
            state = proposal_states[proposal_id - 1]
            proposals[state].append(proposal_id)

        return proposals

    async def fetch_proposal(self, proposal_id: int) -> Proposal:
        proposal_contract = await self._get_proposal_contract()
        (
            proposer,
            message,
            payload,
            created,
            start,
            phase1_end,
            phase2_end,
            expires,
            vp_for_raw,
            vp_against_raw,
            vp_veto_raw,
            vp_abstain_raw,
            vp_required_raw,
            veto_quorum_raw,
        ) = await rp.multicall(
            [
                proposal_contract.functions.getProposer(proposal_id),
                proposal_contract.functions.getMessage(proposal_id),
                proposal_contract.functions.getPayload(proposal_id),
                proposal_contract.functions.getCreated(proposal_id),
                proposal_contract.functions.getStart(proposal_id),
                proposal_contract.functions.getPhase1End(proposal_id),
                proposal_contract.functions.getPhase2End(proposal_id),
                proposal_contract.functions.getExpires(proposal_id),
                proposal_contract.functions.getVotingPowerFor(proposal_id),
                proposal_contract.functions.getVotingPowerAgainst(proposal_id),
                proposal_contract.functions.getVotingPowerVeto(proposal_id),
                proposal_contract.functions.getVotingPowerAbstained(proposal_id),
                proposal_contract.functions.getVotingPowerRequired(proposal_id),
                proposal_contract.functions.getVetoQuorum(proposal_id),
            ]
        )
        return ProtocolDAO.Proposal(
            id=proposal_id,
            proposer=cast(ChecksumAddress, proposer),
            message=message,
            payload=payload,
            created=created,
            start=start,
            end_phase_1=phase1_end,
            end_phase_2=phase2_end,
            expires=expires,
            votes_for=solidity.to_float(vp_for_raw),
            votes_against=solidity.to_float(vp_against_raw),
            votes_veto=solidity.to_float(vp_veto_raw),
            votes_abstain=solidity.to_float(vp_abstain_raw),
            quorum=solidity.to_float(vp_required_raw),
            veto_quorum=solidity.to_float(veto_quorum_raw),
        )

    def _build_vote_graph(self, proposal: DAO.Proposal) -> str:
        assert isinstance(proposal, ProtocolDAO.Proposal)
        graph = tpl.figure()
        graph.barh(
            [
                round(proposal.votes_for),
                round(proposal.votes_against),
                round(proposal.votes_abstain),
                round(max(proposal.votes_total, proposal.quorum)),
            ],
            ["For", "Against", "Abstain", ""],
            max_width=12,
        )
        main_quorum_perc = proposal.votes_total / proposal.quorum

        lines = str(graph.get_string()).split("\n")[:-1]
        lines.append(
            f"Quorum: {main_quorum_perc:.2%}{' ✔' if (main_quorum_perc >= 1) else ''}"
        )

        if proposal.votes_veto > 0:
            graph = tpl.figure()
            graph.barh(
                [
                    round(proposal.votes_veto),
                    round(max(proposal.votes_veto, proposal.veto_quorum)),
                ],
                [f"{'Veto': <{len('Against')}}", ""],
                max_width=12,
            )
            veto_graph_bars = graph.get_string().split("\n")
            veto_quorum_perc = proposal.votes_veto / proposal.veto_quorum

            lines.append("")
            lines.append(f"{veto_graph_bars[0]: <{len(veto_graph_bars[1])}}▏")
            lines.append(
                f"Quorum: {veto_quorum_perc:.2%}{' ✔' if (veto_quorum_perc >= 1) else ''}"
            )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatting helpers for DAO governance embeds
# ---------------------------------------------------------------------------


def _share_repr(percentage: float, max_width: int = 35) -> str:
    num_points = round(max_width * percentage / 100)
    return "*" * num_points


def build_claimer_description(args: dict[str, int]) -> str:
    node_share = args["nodePercent"] / 10**16
    pdao_share = args["protocolPercent"] / 10**16
    odao_share = args["trustedNodePercent"] / 10**16

    return "\n".join(
        [
            "Node Operator Share",
            f"{_share_repr(node_share)} {node_share:.1f}%",
            "Protocol DAO Share",
            f"{_share_repr(pdao_share)} {pdao_share:.1f}%",
            "Oracle DAO Share",
            f"{_share_repr(odao_share)} {odao_share:.1f}%",
        ]
    )


def decode_setting_multi(args: dict[str, list], values_list: list[bytes]) -> str:
    description_parts = []
    for i in range(len(args["settingContractNames"])):
        value_raw = values_list[i]
        match args["types"][i]:
            case 0:
                # SettingType.UINT256
                value = w3.to_int(value_raw)
            case 1:
                # SettingType.BOOL
                value = bool(value_raw)
            case 2:
                # SettingType.ADDRESS
                value = w3.to_checksum_address(value_raw)
            case _:
                value = "???"
        description_parts.append(f"`{args['settingPaths'][i]}` set to `{value}`")
    return "\n".join(description_parts)


async def wrap_member_address(address: ChecksumAddress, block: int) -> str:
    return await el_explorer_url(address, block=block)
