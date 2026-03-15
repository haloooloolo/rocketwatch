import logging
from dataclasses import dataclass
from operator import attrgetter
from typing import Literal

from discord import Interaction
from discord.app_commands import Choice, autocomplete, command, describe
from discord.ext.commands import Cog
from eth_typing import ChecksumAddress
from tabulate import tabulate

from rocketwatch import RocketWatch
from utils import solidity
from utils.block_time import ts_to_block
from utils.dao import DefaultDAO, OracleDAO, ProtocolDAO, SecurityCouncil
from utils.embeds import Embed, el_explorer_url
from utils.event_logs import get_logs
from utils.rocketpool import rp
from utils.views import PageView
from utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.dao")


class OnchainDAO(Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    @staticmethod
    async def get_dao_votes_embed(dao: DefaultDAO, full: bool) -> Embed:
        current_proposals: dict[DefaultDAO.ProposalState, list[DefaultDAO.Proposal]] = {
            dao.ProposalState.Pending: [],
            dao.ProposalState.Active: [],
            dao.ProposalState.Succeeded: [],
        }

        for state, ids in (await dao.get_proposal_ids_by_state()).items():
            if state in current_proposals:
                current_proposals[state].extend(
                    [await dao.fetch_proposal(pid) for pid in ids]
                )

        parts = []
        for proposal in current_proposals[dao.ProposalState.Pending]:
            body = await dao.build_proposal_body(
                proposal,
                include_proposer=full,
                include_votes=False,
                include_payload=full,
            )
            parts.append(
                f"**Proposal #{proposal.id}** - Pending\n```{body}```"
                f"Voting starts <t:{proposal.start}:R>, ends <t:{proposal.end}:R>."
            )
        for proposal in current_proposals[dao.ProposalState.Active]:
            body = await dao.build_proposal_body(
                proposal,
                include_proposer=full,
                include_votes=True,
                include_payload=full,
            )
            parts.append(
                f"**Proposal #{proposal.id}** - Active\n```{body}```Voting ends <t:{proposal.end}:R>."
            )
        for proposal in current_proposals[dao.ProposalState.Succeeded]:
            body = await dao.build_proposal_body(
                proposal,
                include_proposer=full,
                include_votes=full,
                include_payload=full,
            )
            parts.append(
                f"**Proposal #{proposal.id}** - Succeeded (Not Yet Executed)\n```{body}```Expires <t:{proposal.expires}:R>."
            )

        return Embed(
            title=f"{dao.display_name} Proposals",
            description="\n\n".join(parts) or "No active proposals.",
        )

    @staticmethod
    async def get_pdao_votes_embed(dao: ProtocolDAO, full: bool) -> Embed:
        current_proposals: dict[
            ProtocolDAO.ProposalState, list[ProtocolDAO.Proposal]
        ] = {
            dao.ProposalState.Pending: [],
            dao.ProposalState.ActivePhase1: [],
            dao.ProposalState.ActivePhase2: [],
            dao.ProposalState.Succeeded: [],
        }

        for state, ids in (await dao.get_proposal_ids_by_state()).items():
            if state in current_proposals:
                current_proposals[state].extend(
                    [await dao.fetch_proposal(pid) for pid in ids]
                )

        parts = []
        for proposal in current_proposals[dao.ProposalState.Pending]:
            body = await dao.build_proposal_body(
                proposal,
                include_proposer=full,
                include_votes=False,
                include_payload=full,
            )
            parts.append(
                f"**Proposal #{proposal.id}** - Pending\n```{body}```"
                f"Voting starts <t:{proposal.start}:R>, ends <t:{proposal.end_phase_2}:R>."
            )
        for proposal in current_proposals[dao.ProposalState.ActivePhase1]:
            body = await dao.build_proposal_body(
                proposal,
                include_proposer=full,
                include_votes=True,
                include_payload=full,
            )
            parts.append(
                f"**Proposal #{proposal.id}** - Active (Phase 1)\n```{body}```"
                f"Next phase <t:{proposal.end_phase_1}:R>, voting ends <t:{proposal.end_phase_2}:R>."
            )
        for proposal in current_proposals[dao.ProposalState.ActivePhase2]:
            body = await dao.build_proposal_body(
                proposal,
                include_proposer=full,
                include_votes=True,
                include_payload=full,
            )
            parts.append(
                f"**Proposal #{proposal.id}** - Active (Phase 2)\n```{body}```Voting ends <t:{proposal.end_phase_2}:R>."
            )
        for proposal in current_proposals[dao.ProposalState.Succeeded]:
            body = await dao.build_proposal_body(
                proposal,
                include_proposer=full,
                include_votes=full,
                include_payload=full,
            )
            parts.append(
                f"**Proposal #{proposal.id}** - Succeeded (Not Yet Executed)\n```{body}```Expires <t:{proposal.expires}:R>."
            )

        return Embed(
            title="pDAO Proposals",
            description="\n\n".join(parts) or "No active proposals.",
        )

    @command()
    @describe(dao_name="DAO to show proposals for")
    @describe(full="show all information (e.g. payload)")
    async def dao_votes(
        self,
        interaction: Interaction,
        dao_name: Literal["oDAO", "pDAO", "Security Council"] = "pDAO",
        full: bool = False,
    ) -> None:
        """Show currently active on-chain proposals"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        match dao_name:
            case "pDAO":
                dao = ProtocolDAO()
                embed = await self.get_pdao_votes_embed(dao, full)
            case "oDAO":
                dao = OracleDAO()
                embed = await self.get_dao_votes_embed(dao, full)
            case "Security Council":
                dao = SecurityCouncil()
                embed = await self.get_dao_votes_embed(dao, full)
            case _:
                raise ValueError(f"Invalid DAO name: {dao_name}")

        await interaction.followup.send(embed=embed)

    @dataclass(slots=True)
    class Vote:
        voter: ChecksumAddress
        direction: int
        voting_power: float
        time: int

    class VoterPageView(PageView):
        def __init__(self, proposal: ProtocolDAO.Proposal):
            super().__init__(page_size=25)
            self.proposal = proposal
            self._voter_list = None

        async def _ensure_voter_list(self):
            if self._voter_list is not None:
                return
            self._voter_list = await self._get_voter_list(self.proposal)

        async def _get_voter_list(
            self, proposal: ProtocolDAO.Proposal
        ) -> list["OnchainDAO.Vote"]:
            voters: dict[ChecksumAddress, OnchainDAO.Vote] = {}
            dao = ProtocolDAO()
            proposal_contract = await dao._get_proposal_contract()

            for vote_log in await get_logs(
                proposal_contract.events.ProposalVoted,
                await ts_to_block(proposal.start) - 1,
                await ts_to_block(proposal.end_phase_2) + 1,
                {"proposalID": proposal.id},
            ):
                vote = OnchainDAO.Vote(
                    vote_log.args.voter,
                    vote_log.args.direction,
                    solidity.to_float(vote_log.args.votingPower),
                    vote_log.args.time,
                )
                voters[vote.voter] = vote

            for override_log in await get_logs(
                proposal_contract.events.ProposalVoteOverridden,
                await ts_to_block(proposal.end_phase_1) - 1,
                await ts_to_block(proposal.end_phase_2) + 1,
                {"proposalID": proposal.id},
            ):
                voting_power = solidity.to_float(override_log.args.votingPower)
                voters[override_log.args.delegate].voting_power -= voting_power

            return sorted(voters.values(), key=attrgetter("voting_power"), reverse=True)

        @property
        def _title(self) -> str:
            return f"pDAO Proposal #{self.proposal.id} - Voter List"

        async def _load_content(self, from_idx: int, to_idx: int) -> tuple[int, str]:
            await self._ensure_voter_list()
            headers = ["#", "Voter", "Choice", "Weight"]
            data = []
            for i, voter in enumerate(
                self._voter_list[from_idx : (to_idx + 1)], start=from_idx
            ):
                name = (
                    (await el_explorer_url(voter.voter, prefix=-1))
                    .split("[")[1]
                    .split("]")[0]
                )
                vote = ["", "Abstain", "For", "Against", "Veto"][voter.direction]
                voting_power = f"{voter.voting_power:,.2f}"
                data.append([i + 1, name, vote, voting_power])

            if not data:
                return 0, ""

            table = tabulate(data, headers, colalign=("right", "left", "left", "right"))
            return len(self._voter_list), f"```{table}```"

    async def _get_recent_proposals(
        self, interaction: Interaction, current: str
    ) -> list[Choice[int]]:
        dao = ProtocolDAO()
        proposal_contract = await dao._get_proposal_contract()
        num_proposals = await proposal_contract.functions.getTotal().call()

        if current:
            try:
                suggestions = [int(current)]
                assert 1 <= int(current) <= num_proposals
            except (ValueError, AssertionError):
                return []
        else:
            suggestions = list(range(1, num_proposals + 1))[:-26:-1]

        titles: list[str] = await rp.multicall(
            [
                proposal_contract.functions.getMessage(proposal_id)
                for proposal_id in suggestions
            ]
        )
        return [
            Choice(name=f"#{pid}: {title}", value=pid)
            for pid, title in zip(suggestions, titles, strict=False)
        ]

    @command()
    @describe(proposal="proposal to show voters for")
    @autocomplete(proposal=_get_recent_proposals)
    async def voter_list(self, interaction: Interaction, proposal: int) -> None:
        """Show the list of voters for a pDAO proposal"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        if not (proposal := await ProtocolDAO().fetch_proposal(proposal)):
            return await interaction.followup.send("Invalid proposal ID.")

        view = OnchainDAO.VoterPageView(proposal)
        embed = await view.load()
        await interaction.followup.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(OnchainDAO(bot))
