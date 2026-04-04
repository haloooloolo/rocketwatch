import logging
from collections.abc import Sequence
from datetime import datetime, timedelta

from discord import Interaction
from discord.app_commands import command
from discord.utils import escape_markdown
from eth_typing import HexStr
from web3.constants import HASH_ZERO

from rocketwatch.plugins.forum.forum import Forum
from rocketwatch.plugins.rpips.rpips import RPIPs
from rocketwatch.plugins.snapshot.snapshot import Snapshot
from rocketwatch.bot import RocketWatch
from rocketwatch.utils.block_time import ts_to_block
from rocketwatch.utils.config import cfg
from rocketwatch.utils.dao import (
    DAO,
    DefaultDAO,
    OracleDAO,
    ProtocolDAO,
    SecurityCouncil,
)
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.status import StatusPlugin
from rocketwatch.utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.governance")


class Governance(StatusPlugin):
    @staticmethod
    async def _get_active_pdao_proposals(
        dao: ProtocolDAO,
    ) -> list[ProtocolDAO.Proposal]:
        proposal_ids = await dao.get_proposal_ids_by_state()
        active_proposal_ids = []
        active_proposal_ids += proposal_ids[dao.ProposalState.ActivePhase1]
        active_proposal_ids += proposal_ids[dao.ProposalState.ActivePhase2]
        return [
            await dao.fetch_proposal(proposal_id)
            for proposal_id in reversed(active_proposal_ids)
        ]

    @staticmethod
    async def _get_active_dao_proposals(dao: DefaultDAO) -> list[DefaultDAO.Proposal]:
        proposal_ids = await dao.get_proposal_ids_by_state()
        active_proposal_ids = proposal_ids[dao.ProposalState.Active]
        return [
            await dao.fetch_proposal(proposal_id)
            for proposal_id in reversed(active_proposal_ids)
        ]

    @staticmethod
    async def _get_tx_hash_for_proposal(dao: DAO, proposal: DAO.Proposal) -> HexStr:
        from_block = (await ts_to_block(proposal.created)) - 1
        to_block = (await ts_to_block(proposal.created)) + 1

        log.info(f"Looking for proposal {proposal} in [{from_block},{to_block}]")
        if not (proposal_contract := dao._proposal_contract):
            return HASH_ZERO

        for receipt in proposal_contract.events.ProposalAdded().get_logs(
            from_block=from_block, to_block=to_block
        ):
            log.info(f"Found receipt {receipt}")
            if receipt.args.proposalID == proposal.id:
                return HexStr(receipt.transactionHash.hex())

        return HASH_ZERO

    async def _get_active_snapshot_proposals(self) -> list[Snapshot.Proposal]:
        try:
            return list(await Snapshot.fetch_proposals("active", reverse=True))
        except Exception as e:
            await self.bot.report_error(e)
            return []

    async def _get_draft_rpips(self) -> list[RPIPs.RPIP]:
        try:
            statuses = {"Draft", "Review"}
            return [
                rpip
                for rpip in await RPIPs.get_all_rpips()
                if (rpip.status in statuses)
            ][::-1]
        except Exception as e:
            await self.bot.report_error(e)
            return []

    async def _get_latest_forum_topics(self, days: int) -> list[Forum.Topic]:
        try:
            topics = await Forum.get_recent_topics()
            now = datetime.now().timestamp()
            # only get topics from within a week
            topics = [
                t
                for t in topics
                if (now - t.last_post_at) <= timedelta(days=days).total_seconds()
            ]
            return topics
        except Exception as e:
            await self.bot.report_error(e)
            return []

    async def get_digest(self) -> Embed:
        embed = Embed(title="Governance Digest")
        embed.description = ""

        def sanitize(text: str, max_length: int = 50) -> str:
            text = text.strip()
            text = text.replace("http://", "")
            text = text.replace("https://", "")
            text = escape_markdown(text)
            if len(text) > max_length:
                text = text[: (max_length - 1)] + "…"
            return text

        async def print_proposals(_dao: DAO, _proposals: list[DAO.Proposal]) -> str:
            text = ""
            for _i, _proposal in enumerate(_proposals, start=1):
                _title = sanitize(_proposal.message, 40)
                _tx_hash = await self._get_tx_hash_for_proposal(_dao, _proposal)
                _url = f"{cfg.execution_layer.explorer}/tx/{_tx_hash}"
                text += f"  {_i}. [{_title}]({_url}) (#{_proposal.id})\n"
            return text

        # --------- SECURITY COUNCIL --------- #

        dao = SecurityCouncil()
        if proposals := await self._get_active_dao_proposals(dao):
            embed.description += "### Security Council\n"
            embed.description += "- **Active on-chain proposals**\n"
            embed.description += await print_proposals(dao, proposals)

        # --------- ORACLE DAO --------- #

        dao = OracleDAO()
        if proposals := await self._get_active_dao_proposals(dao):
            embed.description += "### Oracle DAO\n"
            embed.description += "- **Active on-chain proposals**\n"
            embed.description += await print_proposals(dao, proposals)

        # --------- PROTOCOL DAO --------- #

        section_content = ""
        dao = ProtocolDAO()

        if proposals := await self._get_active_pdao_proposals(dao):
            section_content += "- **Active on-chain proposals**\n"
            section_content += await print_proposals(dao, proposals)

        if snapshot_proposals := await self._get_active_snapshot_proposals():
            section_content += "- **Active Snapshot proposals**\n"
            for i, proposal in enumerate(snapshot_proposals, start=1):
                title = sanitize(proposal.title)
                section_content += f"  {i}. [{title}]({proposal.url})\n"

        if draft_rpips := await self._get_draft_rpips():
            section_content += "- **RPIPs in review or draft status**\n"
            for i, rpip in enumerate(draft_rpips, start=1):
                title = sanitize(rpip.title, 40)
                section_content += (
                    f"  {i}. [{title}]({rpip.url}) (RPIP-{rpip.number})\n"
                )

        if section_content:
            embed.description += "### Protocol DAO\n"
            embed.description += section_content

        # --------- DAO FORUM --------- #

        num_days = 7
        if topics := await self._get_latest_forum_topics(days=num_days):
            embed.description += "### Forum\n"
            embed.description += f"- **Recently active topics ({num_days}d)**\n"
            for i, topic in enumerate(topics[:10], start=1):
                title = sanitize(topic.title, 40)
                embed.description += f"  {i}. [{title}]({topic.url}) [`{topic.post_count - 1}\u202f💬`]\n"

        if not embed.description:
            embed.set_image(url="https://c.tenor.com/PVf-csSHmu8AAAAd/tenor.gif")

        return embed

    @command()
    async def governance_digest(self, interaction: Interaction) -> None:
        """Get a summary of recent activity in protocol governance"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        embed = await self.get_digest()
        await interaction.followup.send(embed=embed)

    async def get_status(self) -> Embed:
        embed = await self.get_digest()
        embed.title = ":classical_building: Live Governance Digest"
        return embed


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(Governance(bot))
