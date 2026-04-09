import logging
from datetime import datetime
from io import BytesIO
from typing import TypedDict

import matplotlib.axes
import matplotlib.pyplot as plt
import numpy as np
from discord import File, Interaction
from discord.app_commands import command
from discord.ext import commands, tasks
from matplotlib.dates import DateFormatter
from matplotlib.ticker import FuncFormatter

from rocketwatch.bot import RocketWatch
from rocketwatch.utils import solidity
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.shared_w3 import w3
from rocketwatch.utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.apr")


class APRDatapoint(TypedDict):
    block: int
    time: float
    value: float
    effectiveness: float


def to_apr(d1: APRDatapoint, d2: APRDatapoint, effective: bool = True) -> float:
    duration = get_duration(d1, d2)
    period_change = get_period_change(d1, d2, effective)
    return period_change * (365 * 24 * 60 * 60 / duration)


def get_period_change(
    d1: APRDatapoint, d2: APRDatapoint, effective: bool = True
) -> float:
    v = (d2["value"] - d1["value"]) / d1["value"]
    if not effective:
        v /= d2["effectiveness"]
    return v


def get_duration(d1: APRDatapoint, d2: APRDatapoint) -> float:
    return d2["time"] - d1["time"]


class APR(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.task.start()

    async def cog_unload(self) -> None:
        self.task.cancel()

    @tasks.loop(seconds=60)
    async def task(self) -> None:
        # get latest block update from the db
        latest_db_block = await self.bot.db.reth_apr.find_one(sort=[("block", -1)])
        latest_db_block = 0 if latest_db_block is None else latest_db_block["block"]
        cursor_block = (await w3.eth.get_block("latest")).get("number", 0)
        while True:
            # get address of rocketNetworkBalances contract at cursor block
            address = await rp.uncached_get_address_by_name(
                "rocketNetworkBalances", block=cursor_block
            )
            balance_block = await rp.call(
                "rocketNetworkBalances.getBalancesBlock",
                block=cursor_block,
                address=address,
            )
            if balance_block == latest_db_block:
                break
            block_time = (await w3.eth.get_block(balance_block)).get("timestamp", 0)
            # abort if the blocktime is older than 120 days
            if block_time < (datetime.now().timestamp() - 120 * 24 * 60 * 60):
                break
            reth_ratio = solidity.to_float(
                await rp.call("rocketTokenRETH.getExchangeRate", block=cursor_block)
            )
            effectiveness = solidity.to_float(
                await rp.call(
                    "rocketNetworkBalances.getETHUtilizationRate",
                    block=cursor_block,
                    address=address,
                )
            )
            await self.bot.db.reth_apr.insert_one(
                {
                    "block": balance_block,
                    "time": block_time,
                    "value": reth_ratio,
                    "effectiveness": effectiveness,
                }
            )
            cursor_block = balance_block - 1

    @task.before_loop
    async def before_loop(self) -> None:
        await self.bot.wait_until_ready()

    @task.error
    async def on_error(self, err: BaseException) -> None:
        assert isinstance(err, Exception)
        await self.bot.report_error(err)

    @command()
    async def reth_apr(self, interaction: Interaction) -> None:
        """Show the current rETH APR"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        e = Embed()
        e.title = "Current rETH APR"
        e.description = "For some comparisons against other LST: [dune dashboard](https://dune.com/rp_community/lst-comparison)"

        # get the last 30 datapoints
        datapoints = (
            await self.bot.db.reth_apr.find()
            .sort("block", -1)
            .limit(180 + 38)
            .to_list(None)
        )
        if len(datapoints) == 0:
            e.description = "No data available yet."
            return await interaction.followup.send(embed=e)

            # get average meta.NodeFee from db, weighted by meta.NodeOperatorShare
        tmp = await (
            await self.bot.db.minipools.aggregate(
                [
                    {
                        "$match": {
                            "beacon.status": "active_ongoing",
                            "node_fee": {"$ne": None},
                            "node_deposit_balance": {"$ne": None},
                        }
                    },
                    {
                        "$project": {
                            "fee": "$node_fee",
                            "share": {
                                "$multiply": [
                                    {
                                        "$subtract": [
                                            1,
                                            {"$divide": ["$node_deposit_balance", 32]},
                                        ]
                                    },
                                    100,
                                ]
                            },
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "pre_numerator": {"$sum": "$fee"},
                            "numerator": {"$sum": {"$multiply": ["$fee", "$share"]}},
                            "denominator": {"$sum": "$share"},
                            "count": {"$sum": 1},
                        }
                    },
                    {
                        "$project": {
                            "average": {"$divide": ["$numerator", "$denominator"]},
                            "reference_average": {
                                "$divide": ["$pre_numerator", "$count"]
                            },
                            "used_pETH_share": {
                                "$divide": [
                                    {"$divide": ["$denominator", "$count"]},
                                    100,
                                ]
                            },
                        }
                    },
                ]
            )
        ).to_list(length=1)

        node_fee = tmp[0]["average"] if len(tmp) > 0 else 20
        peth_share = tmp[0]["used_pETH_share"] if len(tmp) > 0 else 0.75

        datapoints = sorted(datapoints, key=lambda x: x["time"])
        nan = float("nan")
        x = []
        y: list[float] = []
        y_effectiveness: list[float] = []
        y_virtual: list[float] = []
        y_7d: list[float] = []
        y_7d_claim = None
        y_7d_virtual: list[float] = []
        for i in range(1, len(datapoints)):
            # add the data of the datapoint to the x values, need to parse it to a datetime object
            x.append(datetime.fromtimestamp(datapoints[i]["time"]))

            # add the average APR to the y values
            y.append(to_apr(datapoints[i - 1], datapoints[i]))
            y_virtual.append(to_apr(datapoints[i - 1], datapoints[i], effective=False))

            y_effectiveness.append(datapoints[i]["effectiveness"])

            # calculate the 7 day average
            if i > 8:
                y_7d.append(to_apr(datapoints[i - 9], datapoints[i]))
                y_7d_virtual.append(
                    to_apr(datapoints[i - 9], datapoints[i], effective=False)
                )
                y_7d_claim = get_duration(datapoints[i - 9], datapoints[i]) / (
                    60 * 60 * 24
                )
            else:
                # if we dont have enough data, we dont show it
                y_7d.append(nan)
                y_7d_virtual.append(nan)
        e.add_field(
            name=f"{y_7d_claim:.1f} Day Average rETH APR", value=f"{y_7d[-1]:.2%}"
        )
        e.add_field(
            name=f"{y_7d_claim:.1f} Day Average rETH APR (without Effectiveness Drag, Virtual)",
            value=f"{y_7d_virtual[-1]:.2%}",
            inline=False,
        )
        x_arr = np.array(x)
        fig, ax1 = plt.subplots()
        ax2: matplotlib.axes.Axes = ax1.twinx()

        ax2.plot(
            x_arr,
            y,
            marker="+",
            linestyle="",
            label="Period Average",
            alpha=0.6,
            color="orange",
        )
        # ax2.plot(x_arr, y_virtual, marker="x", linestyle="", label="Period Average (Virtual)", alpha=0.4)
        # ax2.plot(x_arr, y_node_operators, marker="+", linestyle="", label="Node Operator APR", alpha=0.4)
        ax2.plot(
            x_arr,
            y_7d,
            linestyle="-",
            label=f"{y_7d_claim:.1f} Day Average",
            color="orange",
        )
        ax2.plot(
            x_arr,
            y_7d_virtual,
            linestyle="-",
            label=f"{y_7d_claim:.1f} Day Average (Virtual)",
            color="green",
        )
        ax1.plot(
            x_arr,
            y_effectiveness,
            linestyle="--",
            label="Effectiveness",
            alpha=0.7,
            color="royalblue",
        )

        ax1.set_title("Observed rETH APR values")
        ax1.set_xlabel("Date")
        ax1.grid(True)
        ax1.set_xlim(left=x_arr[38])
        ax1.tick_params(axis="x", rotation=45)
        ax1.xaxis.set_major_formatter(DateFormatter("%b %d"))

        ax2.yaxis.set_major_formatter(FuncFormatter(lambda x, loc: f"{x:.1%}"))
        ax1.yaxis.set_major_formatter(FuncFormatter(lambda x, loc: f"{x:.1%}"))
        ax1.set_ylabel("Effectiveness")
        ax2.set_ylabel("APR")
        ax1.set_ylim(top=1)
        ax1.legend(loc="upper left")
        ax2.legend(loc="upper right")

        img = BytesIO()
        fig.tight_layout()
        fig.savefig(img, format="png")
        img.seek(0)
        plt.close(fig)

        e.set_image(url="attachment://reth_apr.png")

        e.add_field(
            name="Current Average Effective Commission",
            value=f"{node_fee:.2%} (Observed pETH Share: {peth_share:.2%})",
            inline=False,
        )

        e.add_field(
            name="Effectiveness", value=f"{y_effectiveness[-1]:.2%}", inline=False
        )
        await interaction.followup.send(embed=e, file=File(img, "reth_apr.png"))

    @command()
    async def node_apr(self, interaction: Interaction) -> None:
        """Show the current node operator APR"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        e = Embed()
        e.title = "Current NO APR"
        e.description = ""

        # get the last 30 datapoints
        datapoints = (
            await self.bot.db.reth_apr.find()
            .sort("block", -1)
            .limit(180 + 38)
            .to_list(None)
        )
        if len(datapoints) == 0:
            e.description = "No data available yet."
            return await interaction.followup.send(embed=e)

            # get average meta.NodeFee from db, weighted by meta.NodeOperatorShare
        tmp = await (
            await self.bot.db.minipools.aggregate(
                [
                    {
                        "$match": {
                            "beacon.status": "active_ongoing",
                            "node_fee": {"$ne": None},
                            "node_deposit_balance": {"$ne": None},
                        }
                    },
                    {
                        "$project": {
                            "fee": "$node_fee",
                            "share": {
                                "$multiply": [
                                    {
                                        "$subtract": [
                                            1,
                                            {"$divide": ["$node_deposit_balance", 32]},
                                        ]
                                    },
                                    100,
                                ]
                            },
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "pre_numerator": {"$sum": "$fee"},
                            "numerator": {"$sum": {"$multiply": ["$fee", "$share"]}},
                            "denominator": {"$sum": "$share"},
                            "count": {"$sum": 1},
                        }
                    },
                    {
                        "$project": {
                            "average": {"$divide": ["$numerator", "$denominator"]},
                            "reference_average": {
                                "$divide": ["$pre_numerator", "$count"]
                            },
                            "used_pETH_share": {
                                "$divide": [
                                    {"$divide": ["$denominator", "$count"]},
                                    100,
                                ]
                            },
                        }
                    },
                ]
            )
        ).to_list(length=1)

        node_fee = tmp[0]["average"] if len(tmp) > 0 else 0.2
        peth_share = tmp[0]["used_pETH_share"] if len(tmp) > 0 else 0.75

        network_settings = await rp.get_contract_by_name(
            "rocketDAOProtocolSettingsNetwork"
        )
        leb4_commission = solidity.to_float(
            await network_settings.functions.getNodeShare().call()
        )

        nan = float("nan")
        datapoints = sorted(datapoints, key=lambda x: x["time"])
        x = []
        y_7d_claim = None
        y_7d_virtual: list[float] = []
        y_7d_node_operators_leb4: list[float] = []
        y_7d_node_operators_leb8_05: list[float] = []
        y_7d_node_operators_leb8_14: list[float] = []
        y_7d_solo: list[float] = []
        for i in range(1, len(datapoints)):
            x.append(datetime.fromtimestamp(datapoints[i]["time"]))

            if i > 8:
                y_7d_virtual.append(
                    to_apr(datapoints[i - 9], datapoints[i], effective=False)
                )
                bare_apr = y_7d_virtual[-1] / (1 - node_fee)
                y_7d_solo.append(bare_apr)
                peth_share_leb4 = 0.875
                y_7d_node_operators_leb4.append(
                    bare_apr
                    * (1 + (leb4_commission * peth_share_leb4 / (1 - peth_share_leb4)))
                )
                peth_share_leb8 = 0.75
                y_7d_node_operators_leb8_05.append(
                    bare_apr * (1 + (0.05 * peth_share_leb8 / (1 - peth_share_leb8)))
                )
                y_7d_node_operators_leb8_14.append(
                    bare_apr * (1 + (0.14 * peth_share_leb8 / (1 - peth_share_leb8)))
                )
                y_7d_claim = round(
                    get_duration(datapoints[i - 9], datapoints[i]) / (60 * 60 * 24)
                )
            else:
                y_7d_solo.append(nan)
                y_7d_node_operators_leb4.append(nan)
                y_7d_node_operators_leb8_05.append(nan)
                y_7d_node_operators_leb8_14.append(nan)
        e.add_field(
            name=f"{y_7d_claim} Day Average Node Operator APR:",
            value=f"**leb4 {leb4_commission:.0%}:** `{y_7d_node_operators_leb4[-1]:.2%}`\n"
            f"**leb8 5%:** `{y_7d_node_operators_leb8_05[-1]:.2%}` | "
            f"**leb8 14%:** `{y_7d_node_operators_leb8_14[-1]:.2%}`",
            inline=False,
        )

        x_arr = np.array(x)
        fig, ax1 = plt.subplots()

        ax1.plot(
            x_arr,
            y_7d_node_operators_leb4,
            linestyle="-",
            label=f"{y_7d_claim} Day Average (leb4 {leb4_commission:.0%})",
            color="orange",
        )
        ax1.plot(
            x_arr,
            y_7d_node_operators_leb8_05,
            linestyle="--",
            label=f"{y_7d_claim} Day Average (leb8 5%)",
            color="red",
            alpha=0.7,
        )
        ax1.plot(
            x_arr,
            y_7d_node_operators_leb8_14,
            linestyle="-.",
            label=f"{y_7d_claim:.1f} Day Average (leb8 14%)",
            color="red",
            alpha=0.5,
        )
        ax1.plot(
            x_arr,
            y_7d_solo,
            linestyle=":",
            label=f"{y_7d_claim:.1f} Day Average (solo)",
            color="black",
            alpha=0.5,
        )

        ax1.set_title("Observed NO APR values")
        ax1.grid(True)
        ax1.set_xlim(left=x_arr[38])
        ax1.tick_params(axis="x", rotation=0)
        ax1.set_ylim(bottom=0.02)
        ax1.xaxis.set_major_formatter(DateFormatter("%m.%d"))

        ax1.yaxis.set_major_formatter(FuncFormatter(lambda x, loc: f"{x:.1%}"))
        ax1.legend(loc="lower left")

        img = BytesIO()
        fig.tight_layout()
        fig.savefig(img, format="png")
        img.seek(0)
        plt.close(fig)

        e.add_field(
            name="Current Average Effective Commission:",
            value=f"{node_fee:.2%} (Observed pETH Share: {peth_share:.2%})",
            inline=False,
        )

        e.set_image(url="attachment://no_apr.png")

        await interaction.followup.send(embed=e, file=File(img, "no_apr.png"))


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(APR(bot))
