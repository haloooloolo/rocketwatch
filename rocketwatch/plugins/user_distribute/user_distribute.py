import logging

from discord.ext import commands
from discord.ext.commands import Context, hybrid_command
from pymongo import AsyncMongoClient, ASCENDING

import time
from rocketwatch import RocketWatch
from utils.rocketpool import rp
from utils.cfg import cfg
from utils.embeds import Embed, el_explorer_url
from utils.shared_w3 import w3, bacon
from utils.views import PageView
from utils.visibility import is_hidden_weak

log = logging.getLogger("user_distribute")
log.setLevel(cfg["log_level"])


class UserDistribute(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.db = AsyncMongoClient(cfg["mongodb.uri"]).get_database("rocketwatch")

    @hybrid_command()
    async def minipool_user_distribute(self, ctx: Context):
        """Show user distribute summary for minipools"""
        await ctx.defer(ephemeral=is_hidden_weak(ctx))

        head = await bacon.get_block_header_async("head")
        current_epoch = int(head["data"]["header"]["message"]["slot"]) // 32
        threshold_epoch = current_epoch - 5000

        minipools = await self.db.minipools.find({
            "user_distributed": False,
            "status": "staking",
            "execution_balance": {"$gte": 8},
            "beacon.withdrawable_epoch": {"$lt": threshold_epoch}
        }).sort("beacon.withdrawable_epoch", ASCENDING).to_list()

        eligible = []
        pending = []
        distributable = []

        min_pending_time = 2 ** 256
        min_distributable_time = 2 ** 256

        current_time = int(time.time())
        ud_window_start = rp.call("rocketDAOProtocolSettingsMinipool.getUserDistributeWindowStart")
        ud_window_end = ud_window_start + rp.call("rocketDAOProtocolSettingsMinipool.getUserDistributeWindowLength")

        for mp in minipools:
            mp_address = w3.to_checksum_address(mp["address"])
            storage = w3.eth.get_storage_at(mp_address, 0x17)
            user_distribute_time = int.from_bytes(storage, "big")
            elapsed_time = current_time - user_distribute_time

            if elapsed_time >= ud_window_end:
                eligible.append(mp)
            elif elapsed_time < ud_window_start:
                min_pending_time = min(ud_window_start, min_pending_time)
                pending.append(mp)
            elif not rp.call("rocketMinipoolDelegate.getUserDistributed", address=mp_address): # double check, DB may lag behind
                min_distributable_time = min(ud_window_end, min_distributable_time)
                distributable.append(mp)

        embed = Embed(title="User Distribute Status")

        embed.add_field(
            name="Eligible",
            value=f"**{len(eligible)}** minipool{'s' if len(eligible) != 1 else ''}",
            inline=False
        )

        if pending:
            earliest_ts = current_time + min_pending_time
            embed.add_field(
                name="Pending",
                value=f"**{len(pending)}** minipool{'s' if len(pending) != 1 else ''} · first window opens <t:{earliest_ts}:R>",
                inline=False
            )
        else:
            embed.add_field(name="Pending", value="**0** minipools", inline=False)

        if distributable:
            closes_ts = current_time + min_distributable_time
            embed.add_field(
                name="Distributable",
                value=f"**{len(distributable)}** minipool{'s' if len(distributable) != 1 else ''} · first window closes <t:{closes_ts}:R>",
                inline=False
            )
        else:
            embed.add_field(name="Distributable", value="**0** minipools", inline=False)

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(UserDistribute(bot))
