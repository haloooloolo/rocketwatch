import logging

from pymongo import AsyncMongoClient

from discord import Interaction
from discord.ext import commands
from discord.app_commands import command

from rocketwatch import RocketWatch
from utils.embeds import Embed, el_explorer_url
from utils.readable import s_hex
from utils.shared_w3 import w3
from utils.cfg import cfg
from utils.rocketpool import rp

log = logging.getLogger("minipool_delegates")
log.setLevel(cfg["log_level"])

    
class MinipoolDelegates(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.db = AsyncMongoClient(cfg["mongodb.uri"]).rocketwatch
    
    @command()
    async def minipool_delegates(self, interaction: Interaction):
        """Show stats for minipool delegate adoption"""
        await interaction.response.defer()
        # only consider active minipools
        minipool_filter = {"beacon.status": {"$in": ["pending_initialized", "pending_queued", "active_ongoing"]}}
        # we want to show the distribution of minipools that are using each delegate
        distribution_stats = await (await self.db.minipools.aggregate([
            {"$match": minipool_filter},
            {"$group": {"_id": "$effective_delegate", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
        ])).to_list()
        # and the percentage of minipools that are using the useLatestDelegate flag
        use_latest_delegate_stats = await (await self.db.minipools.aggregate([
            {"$match": minipool_filter},
            {"$group": {"_id": "$use_latest_delegate", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
        ])).to_list()
        e = Embed()
        e.title = "Minipool Delegate Stats"
        desc = "**Effective Delegate Distribution:**\n"
        c_sum = sum(d['count'] for d in distribution_stats)
        s = "\u00A0" * 4
        # latest delegate acording to rp
        rp.uncached_get_address_by_name("rocketMinipoolDelegate")
        for d in distribution_stats:
            # I HATE THE CHECKSUMMED ADDRESS REQUIREMENTS I HATE THEM SO MUCH
            a = w3.to_checksum_address(d['_id'])
            name = s_hex(a)
            if a == rp.get_address_by_name("rocketMinipoolDelegate"):
                name += " (Latest)"
            desc += f"{s}{el_explorer_url(a, name)}: {d['count']:,} ({d['count'] / c_sum * 100:.2f}%)\n"
        desc += "\n"
        desc += "**Use Latest Delegate:**\n"
        c_sum = sum(d['count'] for d in use_latest_delegate_stats)
        for d in use_latest_delegate_stats:
            # true = yes, false = no
            d['_id'] = "Yes" if d['_id'] else "No"
            desc += f"{s}**{d['_id']}**: {d['count']:,} ({d['count'] / c_sum * 100:.2f}%)\n"
        e.description = desc
        await interaction.followup.send(embed=e)
        
        
async def setup(self):
    await self.add_cog(MinipoolDelegates(self))
