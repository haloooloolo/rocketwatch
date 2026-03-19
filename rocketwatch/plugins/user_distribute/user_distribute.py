import logging
import time
from io import StringIO
from operator import itemgetter

import discord
from discord import ButtonStyle, Interaction, ui
from discord.app_commands import command
from discord.ext import commands, tasks
from pymongo import ASCENDING

from rocketwatch import RocketWatch
from utils.config import cfg
from utils.embeds import Embed
from utils.rocketpool import rp
from utils.shared_w3 import bacon, w3
from utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.user_distribute")


class InstructionsView(ui.View):
    def __init__(
        self, eligible: list[dict], distributable: list[dict], instruction_timeout: int
    ):
        super().__init__(timeout=instruction_timeout)
        self.eligible = eligible
        self.distributable = distributable

    @ui.button(label="Instructions", style=ButtonStyle.blurple)
    async def instructions(self, interaction: Interaction, _) -> None:
        mp_contract = await rp.assemble_contract("rocketMinipoolDelegate")
        bud_calldata = bytes.fromhex(
            mp_contract.encode_abi(abi_element_identifier="beginUserDistribute")[2:]
        )
        dist_calldata = bytes.fromhex(
            mp_contract.encode_abi(
                abi_element_identifier="distributeBalance", args=[False]
            )[2:]
        )

        calls = [(mp["address"], False, dist_calldata) for mp in self.distributable]
        calls += [(mp["address"], False, bud_calldata) for mp in self.eligible]

        multicall_contract = await rp.get_contract_by_name("multicall3")
        gas_used = await multicall_contract.functions.aggregate3(calls).estimate_gas()
        gas_price = await w3.eth.gas_price
        cost_eth = gas_used * gas_price / 1e18

        tuple_strs = []
        for address, allow_failure, calldata in calls:
            tuple_strs.append(
                f'["{address}", {str(allow_failure).lower()}, "0x{calldata.hex()}"]'
            )

        input_data = "[" + ",".join(tuple_strs) + "]"
        etherscan_url = f"https://etherscan.io/address/{multicall_contract.address}#writeContract#F2"

        embed = Embed(title="Distribution Instructions")
        embed.description = (
            f"1. Open the [Multicall `aggregate3` function]({etherscan_url}) on Etherscan\n"
            f"2. Enter `0` for `payableAmount (ether)`\n"
            f"3. Paste the provided input data into the `calls (tuple[])` field\n"
            f"4. Connect your wallet (`Connect to Web3`)\n"
            f"5. Click `Write` and sign with your wallet\n"
        )

        actions = []
        if (count := len(self.distributable)) > 0:
            actions.append(
                f"distribute the balance of **{count}** minipool{'s' if count != 1 else ''}"
            )
        if (count := len(self.eligible)) > 0:
            actions.append(
                f"begin the user distribution process for **{count}** minipool{'s' if count != 1 else ''}"
            )

        embed.description += "\nThis will " + " and ".join(actions) + "."
        embed.description += f"\nEstimated cost: **{cost_eth:,.6f} ETH** ({gas_used:,} gas @ {(gas_price / 1e9):.2f} gwei)"

        await interaction.response.send_message(
            embed=embed,
            file=discord.File(StringIO(input_data), filename="input_data.txt"),
            ephemeral=True,
        )


class UserDistribute(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.task.start()

    async def cog_unload(self):
        self.task.cancel()

    @tasks.loop(hours=8)
    async def task(self):
        channel_id = cfg.discord.channels.get("user_distribute")
        if not channel_id:
            return

        channel = await self.bot.get_or_fetch_channel(channel_id)

        _, _, distributable = await self._fetch_minipools()
        if not distributable:
            return

        embed = Embed(title=":hourglass_flowing_sand: User Distribution Window Open")
        count = len(distributable)
        next_window_close = distributable[0]["ud_window_close"]
        embed.description = (
            f"There {'are' if count != 1 else 'is'} **{count}**"
            f" minipool{'s' if count != 1 else ''} eligible for distribution.\n"
            f"The next window closes <t:{next_window_close}:R>!"
        )

        await channel.send(
            embed=embed,
            view=InstructionsView(
                [], distributable[:100], instruction_timeout=(4 * 3600)
            ),
        )

    @task.before_loop
    async def before_task(self):
        await self.bot.wait_until_ready()

    @task.error
    async def on_task_error(self, err: Exception):
        await self.bot.report_error(err)

    async def _fetch_minipools(self) -> tuple[list[dict], list[dict], list[dict]]:
        head = await bacon.get_block_header("head")
        current_epoch = int(head["data"]["header"]["message"]["slot"]) // 32
        threshold_epoch = current_epoch - 5000

        minipools = (
            await self.bot.db.minipools.find(
                {
                    "user_distributed": False,
                    "status": "staking",
                    "execution_balance": {"$gte": 8},
                    "beacon.withdrawable_epoch": {"$lt": threshold_epoch},
                }
            )
            .sort("beacon.withdrawable_epoch", ASCENDING)
            .to_list()
        )

        eligible = []
        pending = []
        distributable = []

        current_time = int(time.time())
        ud_window_start = await rp.call(
            "rocketDAOProtocolSettingsMinipool.getUserDistributeWindowStart"
        )
        ud_window_end = ud_window_start + await rp.call(
            "rocketDAOProtocolSettingsMinipool.getUserDistributeWindowLength"
        )

        for mp in minipools:
            mp["address"] = w3.to_checksum_address(mp["address"])
            storage = await w3.eth.get_storage_at(mp["address"], 0x17)
            user_distribute_time: int = int.from_bytes(storage, "big")
            elapsed_time = current_time - user_distribute_time

            if elapsed_time >= ud_window_end:
                eligible.append(mp)
            elif elapsed_time < ud_window_start:
                mp["ud_window_open"] = user_distribute_time + ud_window_start
                pending.append(mp)
            # double check, DB may lag behind
            elif not await rp.call(
                "rocketMinipoolDelegate.getUserDistributed", address=mp["address"]
            ):
                mp["ud_window_close"] = user_distribute_time + ud_window_end
                distributable.append(mp)

        pending.sort(key=itemgetter("ud_window_open"))
        distributable.sort(key=itemgetter("ud_window_close"))

        return eligible, pending, distributable

    @command()
    async def user_distribute_status(self, interaction: Interaction):
        """Show user distribute summary for minipools"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        eligible, pending, distributable = await self._fetch_minipools()

        embed = Embed(title="User Distribute Status")

        embed.add_field(
            name="Eligible",
            value=f"**{len(eligible)}** minipool{'s' if len(eligible) != 1 else ''}",
            inline=False,
        )

        if pending:
            next_window_open = pending[0]["ud_window_open"]
            embed.add_field(
                name="Pending",
                value=(
                    f"**{len(pending)}** minipool{'s' if len(pending) != 1 else ''}"
                    f" · next window opens <t:{next_window_open}:R>"
                ),
                inline=False,
            )
        else:
            embed.add_field(name="Pending", value="**0** minipools", inline=False)

        if distributable:
            next_window_close = distributable[0]["ud_window_close"]
            embed.add_field(
                name="Distributable",
                value=(
                    f"**{len(distributable)}** minipool{'s' if len(distributable) != 1 else ''}"
                    f" · next window closes <t:{next_window_close}:R>"
                ),
                inline=False,
            )
        else:
            embed.add_field(name="Distributable", value="**0** minipools", inline=False)

        if eligible or distributable:
            # limit the number of distributions to not run out of gas
            await interaction.followup.send(
                embed=embed,
                view=InstructionsView(
                    eligible[:50], distributable[:100], instruction_timeout=300
                ),
            )
        else:
            await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(UserDistribute(bot))
