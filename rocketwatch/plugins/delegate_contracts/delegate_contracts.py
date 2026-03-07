import logging

from discord import Interaction
from discord.app_commands import command
from discord.ext import commands
from pymongo.asynchronous.collection import AsyncCollection

from rocketwatch import RocketWatch
from utils.cfg import cfg
from utils.embeds import Embed, el_explorer_url
from utils.readable import s_hex
from utils.rocketpool import rp
from utils.shared_w3 import w3

log = logging.getLogger("delegate_contracts")
log.setLevel(cfg.log_level)


class DelegateContracts(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    async def _delegate_stats(
        self,
        collection: AsyncCollection,
        match_filter: dict,
        delegate_field: str,
        use_latest_field: str,
        latest_contract: str,
        title: str,
    ) -> Embed:
        distribution_stats = await (await collection.aggregate([
            {"$match": match_filter},
            {"$group": {"_id": f"${delegate_field}", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
        ])).to_list()

        use_latest_counts = {True: 0, False: 0}
        for d in await (await collection.aggregate([
            {"$match": match_filter},
            {"$group": {"_id": f"${use_latest_field}", "count": {"$sum": 1}}},
        ])).to_list():
            use_latest_counts[bool(d["_id"])] = d["count"]

        e = Embed()
        e.title = title
        s = "\u00A0" * 4
        desc = "**Effective Delegate Distribution:**\n"
        c_sum = sum(d["count"] for d in distribution_stats)
        # refresh cached address
        await rp.uncached_get_address_by_name(latest_contract)
        latest_addr = await rp.get_address_by_name(latest_contract)
        for d in distribution_stats:
            a = w3.to_checksum_address(d["_id"])
            name = s_hex(a)
            if a == latest_addr:
                name += " (Latest)"
            desc += f"{s}{await el_explorer_url(a, name)}: {d['count']:,} ({d['count'] / c_sum * 100:.2f}%)\n"
        desc += "\n"
        desc += "**Use Latest Delegate:**\n"
        c_sum = sum(use_latest_counts.values())
        for value, label in [(True, "Yes"), (False, "No")]:
            count = use_latest_counts[value]
            desc += f"{s}**{label}**: {count:,} ({count / c_sum * 100:.2f}%)\n"
        e.description = desc
        return e

    @command()
    async def minipool_delegates(self, interaction: Interaction):
        """Show stats for minipool delegate contract adoption"""
        await interaction.response.defer()
        e = await self._delegate_stats(
            collection=self.bot.db.minipools,
            match_filter={"beacon.status": {"$in": ["pending_initialized", "pending_queued", "active_ongoing"]}},
            delegate_field="effective_delegate",
            use_latest_field="use_latest_delegate",
            latest_contract="rocketMinipoolDelegate",
            title="Minipool Delegate Stats",
        )
        await interaction.followup.send(embed=e)

    @command()
    async def megapool_delegates(self, interaction: Interaction):
        """Show stats for megapool delegate contract adoption"""
        await interaction.response.defer()
        e = await self._delegate_stats(
            collection=self.bot.db.node_operators,
            match_filter={"megapool.active_validator_count": {"$gt": 0}},
            delegate_field="megapool.effective_delegate",
            use_latest_field="megapool.use_latest_delegate",
            latest_contract="rocketMegapoolDelegate",
            title="Megapool Delegate Stats",
        )
        await interaction.followup.send(embed=e)


async def setup(self):
    await self.add_cog(DelegateContracts(self))
