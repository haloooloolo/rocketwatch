import time
import logging
from io import StringIO
from typing import Optional

import discord
from discord import ui, ButtonStyle, Interaction
from discord.ext import commands, tasks
from discord.ext.commands import Context, hybrid_command
from pymongo import AsyncMongoClient, ASCENDING

from rocketwatch import RocketWatch
from utils.rocketpool import rp
from utils.cfg import cfg
from utils.embeds import Embed
from utils.shared_w3 import w3, bacon
from utils.views import PageView
from utils.visibility import is_hidden_weak

log = logging.getLogger("user_distribute")
log.setLevel(cfg["log_level"])

class InstructionsView(ui.View):
    def __init__(self, eligible: list[dict], distributable: list[dict], instruction_timeout: int):
        super().__init__(timeout=instruction_timeout)
        self.eligible = eligible
        self.distributable = distributable

    @ui.button(label="Instructions", style=ButtonStyle.blurple)
    async def instructions(self, interaction: Interaction, _) -> None:
        mp_contract = rp.assemble_contract("rocketMinipoolDelegate")
        bud_calldata = bytes.fromhex(mp_contract.encodeABI(fn_name="beginUserDistribute")[2:])
        dist_calldata = bytes.fromhex(mp_contract.encodeABI(fn_name="distributeBalance", args=[False])[2:])

        tuple_strs = []
        for mp in self.distributable:
            tuple_strs.append(f"[\"{mp['address']}\", true, 0x{dist_calldata.hex()}]")
        for mp in self.eligible:
            tuple_strs.append(f"[\"{mp['address']}\", true, 0x{bud_calldata.hex()}]")
            
        input_data = "[" + ",".join(tuple_strs) + "]"
                
        etherscan_url = "https://etherscan.io/address/0xcA11bde05977b3631167028862bE2a173976CA11#writeContract#F2"
        
        embed = Embed(title="Distribution Instructions")
        embed.description = (
            f"1. Open the [Multicall `aggregate3` function]({etherscan_url}) on Etherscan\n"
            f"2. Enter `0` for `payableAmount (ether)`\n"
            f"3. Paste the provided input data into the `calls (tuple[])` field\n"
            f"4. Connect your wallet (`Connect to Web3`)\n"
            f"5. Click `Write` and sign with your wallet\n"
        )
        
        actions = []
        if self.distributable:
            actions.append(f"distribute the balance of **{len(self.eligible)}** minipools")
        
        if self.eligible:
            actions.append(f"begin the user distribution process for **{len(self.eligible)}** minipools")
        
        embed.description += "\nThis will " + " and ".join(actions) + "."
        
        await interaction.response.send_message(
            embed=embed,
            file=discord.File(StringIO(input_data), filename="input_data.txt"),
            ephemeral=True
        )


class UserDistribute(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.db = AsyncMongoClient(cfg["mongodb.uri"]).get_database("rocketwatch")
        self.task.start()

    async def cog_unload(self):
        self.task.cancel()

    @tasks.loop(hours=8)
    async def task(self):
        channel_id = cfg.get("discord.channels.user_distribute")
        if not channel_id:
            return
        
        channel = await self.bot.get_or_fetch_channel(channel_id)
        
        _, _, distributable = await self._fetch_minipools()
        if not distributable:
            return

        embed = Embed(title=":warning: User Distribution Window Open")
        next_window_close = min(mp["ud_window_close"] for mp in distributable)
        embed.description = (
            f"There are **{len(distributable)}** minipools eligible for distribution.\n"
            f"The next window closes <t:{next_window_close}:R>!"
        )
        await channel.send(embed=embed, view=InstructionsView([], distributable[:100], instruction_timeout=(4 * 3600)))

    @task.before_loop
    async def before_task(self):
        await self.bot.wait_until_ready()

    @task.error
    async def on_task_error(self, err: Exception):
        await self.bot.report_error(err)
                
    async def _fetch_minipools(self) -> tuple[list[dict], list[dict], list[dict]]:
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

        current_time = int(time.time())
        ud_window_start = rp.call("rocketDAOProtocolSettingsMinipool.getUserDistributeWindowStart")
        ud_window_end = ud_window_start + rp.call("rocketDAOProtocolSettingsMinipool.getUserDistributeWindowLength")

        for mp in minipools:
            mp["address"] = w3.to_checksum_address(mp["address"])
            storage = w3.eth.get_storage_at(mp["address"], 0x17)
            user_distribute_time: int = int.from_bytes(storage, "big")
            elapsed_time = current_time - user_distribute_time
                        
            if elapsed_time >= ud_window_end:
                eligible.append((mp, user_distribute_time))
            elif elapsed_time < ud_window_start:
                mp["ud_window_open"] = user_distribute_time + ud_window_start
                pending.append(mp)
            elif not rp.call("rocketMinipoolDelegate.getUserDistributed", address=mp["address"]): # double check, DB may lag behind
                mp["ud_window_close"] = user_distribute_time + ud_window_end
                distributable.append(mp)
                
        return eligible, pending, distributable

    @hybrid_command()
    async def minipool_user_distribute(self, ctx: Context):
        """Show user distribute summary for minipools"""
        await ctx.defer(ephemeral=is_hidden_weak(ctx))

        eligible, pending, distributable = await self._fetch_minipools()
        
        embed = Embed(title="User Distribute Status")
        
        embed.add_field(
            name="Eligible",
            value=f"**{len(eligible)}** minipool{'s' if len(eligible) != 1 else ''}",
            inline=False
        )

        if pending:
            next_window_open = min(mp["ud_window_open"] for mp in pending)
            embed.add_field(
                name="Pending",
                value=f"**{len(pending)}** minipool{'s' if len(pending) != 1 else ''} · next window opens <t:{next_window_open}:R>",
                inline=False
            )
        else:
            embed.add_field(name="Pending", value="**0** minipools", inline=False)

        if distributable:
            next_window_close = min(mp["ud_window_close"] for mp in distributable)
            embed.add_field(
                name="Distributable",
                value=f"**{len(distributable)}** minipool{'s' if len(distributable) != 1 else ''} · next window closes <t:{next_window_close}:R>",
                inline=False
            )
        else:
            embed.add_field(name="Distributable", value="**0** minipools", inline=False)
                
        if eligible or distributable:
            # limit the number of distributions to not run out of gas
            await ctx.send(embed=embed, view=InstructionsView(eligible[:50], distributable[:100], instruction_timeout=300))
        else:
            await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(UserDistribute(bot))
