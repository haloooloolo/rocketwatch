import functools
import logging
import operator
from io import BytesIO
from typing import Any

import matplotlib as mpl
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
from discord import File, Interaction
from discord.app_commands import command, describe
from discord.ext import commands
from eth_typing import ChecksumAddress
from matplotlib.ticker import FuncFormatter
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch import RocketWatch
from utils import solidity
from utils.embeds import Embed, ens, resolve_ens
from utils.rocketpool import rp
from utils.shared_w3 import w3
from utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.collateral")


def get_percentiles(percentiles, counts):
    for p in percentiles:
        yield p, np.percentile(counts, p, method="nearest")


async def collateral_distribution_raw(interaction: Interaction, distribution):
    e = Embed()
    e.title = "Collateral Distribution"
    description = "```\n"
    for collateral, nodes in distribution:
        description += (
            f"{collateral:>5}%: {nodes:>4} {'node' if nodes == 1 else 'nodes'}\n"
        )
    description += "```"
    e.description = description
    await interaction.followup.send(embed=e)


async def get_node_collateral_data(
    db: AsyncDatabase,
) -> dict[ChecksumAddress, dict[str, int | float]]:
    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "$or": [
                    {"staking_minipool_count": {"$gt": 0}},
                    {"megapool.active_validator_count": {"$gt": 0}},
                ]
            }
        },
        {
            "$project": {
                "address": 1,
                "rpl_stake": {"$ifNull": ["$rpl.total_stake", 0]},
                "bonded": {
                    "$add": [
                        {
                            "$multiply": [
                                {"$ifNull": ["$effective_node_share", 0]},
                                {"$ifNull": ["$staking_minipool_count", 0]},
                                32,
                            ]
                        },
                        {"$ifNull": ["$megapool.node_bond", 0]},
                    ]
                },
                "borrowed": {
                    "$add": [
                        {
                            "$multiply": [
                                {
                                    "$subtract": [
                                        1,
                                        {"$ifNull": ["$effective_node_share", 0]},
                                    ]
                                },
                                {"$ifNull": ["$staking_minipool_count", 0]},
                                32,
                            ]
                        },
                        {"$ifNull": ["$megapool.user_capital", 0]},
                    ]
                },
                "validators": {
                    "$add": [
                        {"$ifNull": ["$staking_minipool_count", 0]},
                        {"$ifNull": ["$megapool.active_validator_count", 0]},
                    ]
                },
            }
        },
    ]
    results = await (await db.node_operators.aggregate(pipeline)).to_list()
    return {
        doc["address"]: {
            "bonded": doc["bonded"],
            "borrowed": doc["borrowed"],
            "rpl_stake": doc["rpl_stake"],
            "validators": doc["validators"],
        }
        for doc in results
    }


async def get_average_collateral_percentage_per_node(
    db: AsyncDatabase, collateral_cap: int | None, bonded: bool
):
    stakes = list((await get_node_collateral_data(db)).values())
    rpl_price = solidity.to_float(await rp.call("rocketNetworkPrices.getRPLPrice"))

    node_collaterals = []
    for node in stakes:
        eth_value = node["bonded"] if bonded else node["borrowed"]
        if not eth_value:
            continue
        rpl_stake = node["rpl_stake"]
        collateral = rpl_stake * rpl_price / eth_value * 100
        if collateral_cap:
            collateral = min(collateral, collateral_cap)
        node_collaterals.append((rpl_stake, collateral))

    effective_bound = max(perc for rpl, perc in node_collaterals)
    possible_step_sizes = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100]
    step_size = possible_step_sizes[
        np.argmin([abs(effective_bound / 30 - s) for s in possible_step_sizes])
    ]

    result: dict[float, list[float]] = {}
    for rpl_stake, percentage in node_collaterals:
        percentage = step_size * (percentage * 10 // (step_size * 10))
        if percentage not in result:
            result[percentage] = []
        result[percentage].append(rpl_stake)

    return result


class Collateral(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    @command()
    @describe(
        node_address="Address or ENS of node to highlight",
        bonded="Calculate collateral as a percent of bonded eth instead of borrowed",
    )
    async def node_tvl_vs_collateral(
        self,
        interaction: Interaction,
        node_address: str | None = None,
        bonded: bool = False,
    ) -> None:
        """
        Show a scatter plot of collateral ratios for given node TVLs
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        display_name = None
        address = None
        if node_address is not None:
            display_name, address = await resolve_ens(interaction, node_address)
            if display_name is None:
                return

        rpl_price = solidity.to_float(await rp.call("rocketNetworkPrices.getRPLPrice"))
        data = await get_node_collateral_data(self.bot.db)

        def node_collateral(node):
            eth = node["bonded"] if bonded else node["borrowed"]
            if not eth:
                return 0
            return 100 * node["rpl_stake"] * rpl_price / eth

        x, y, c = [], [], []
        max_validators = 0
        for node in data.values():
            if not node["bonded"]:
                continue

            x.append(node["bonded"])
            y.append(node_collateral(node))
            c.append(int(node["validators"]))
            max_validators = max(max_validators, int(node["validators"]))

        e = Embed()
        img = BytesIO()
        fig, (ax, ax2) = plt.subplots(2)
        fig.set_figheight(fig.get_figheight() * 2)

        # create the scatter plot
        paths = ax.scatter(x, y, c=c, alpha=0.25, norm="log")
        polys = ax2.hexbin(x, y, gridsize=20, bins="log", xscale="log", cmap="viridis")
        # fill the background in with the default color.
        ax2.set_facecolor(mcolors.to_rgba(mpl.colormaps["viridis"](0), 0.9))
        max_nodes = max(polys.get_array())

        # log-scale the X-axis to account for thomas
        ax.set_xscale("log", base=8)

        # Add a legend for the color-coding on the scatter plot
        formatToInt = "{x:.0f}"
        cb = fig.colorbar(mappable=paths, ax=ax, format=formatToInt)
        cb.set_label("Validator Count")
        cb.set_ticks([1, 10, 100, max_validators])

        # Add a legend for the color-coding on the hex distribution
        cb = fig.colorbar(mappable=polys, ax=ax2, format=formatToInt)
        cb.set_label("Nodes")
        cb.set_ticks([1, 10, 100, max_nodes - 1])

        # Add labels and units
        ylabel = f"Collateral (percent {'bonded' if bonded else 'borrowed'})"
        ax.set_ylabel(ylabel)
        ax2.set_ylabel(ylabel)
        ax.yaxis.set_major_formatter(formatToInt + "%")
        ax2.yaxis.set_major_formatter(formatToInt + "%")
        ax2.set_xlabel("Node Bond (Eth only - log scale)")
        ax.xaxis.set_major_formatter(formatToInt)
        ax2.xaxis.set_major_formatter(formatToInt)

        # Add a red dot if the user asked to highlight their node
        if address is not None:
            # Print a vline and hline through the requested node
            try:
                target_node = data[address]
                ax.plot(target_node["bonded"], node_collateral(target_node), "ro")
                ax2.plot(target_node["bonded"], node_collateral(target_node), "ro")
                e.description = f"Showing location of {display_name}"
            except KeyError:
                await interaction.followup.send(
                    f"{display_name} not found in data set - it must have at least one validator"
                )
                return

        # Add horizontal lines showing the 10-15% range made optimal by RPIP-30
        if not bonded:
            ax.axhspan(10, 15, alpha=0.1, color="grey")

        fig.tight_layout()

        img = BytesIO()
        fig.savefig(img, format="png")
        img.seek(0)
        fig.clear()
        plt.close()

        e.title = "Node TVL vs Collateral Scatter Plot"
        e.set_image(url="attachment://graph.png")
        f = File(img, filename="graph.png")
        await interaction.followup.send(embed=e, files=[f])
        img.close()

    @command()
    @describe(
        raw="Show raw distribution data",
        collateral_cap="Bound the plot at a specific collateral percentage",
        bonded="Calculate collateral as percent of bonded eth instead of borrowed",
    )
    async def collateral_distribution(
        self,
        interaction: Interaction,
        raw: bool = False,
        collateral_cap: int = 15,
        bonded: bool = False,
    ) -> None:
        """
        Show the distribution of collateral across nodes.
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        data = await get_average_collateral_percentage_per_node(
            self.bot.db, collateral_cap, bonded
        )
        distribution = [
            (collateral, len(nodes))
            for collateral, nodes in sorted(data.items(), key=lambda x: x[0])
        ]
        counts: list[float] = functools.reduce(
            operator.iadd,
            ([collateral] * num_nodes for collateral, num_nodes in distribution),
            [],
        )

        # If the raw data were requested, print them and exit early
        if raw:
            await collateral_distribution_raw(interaction, distribution[::-1])
            return

        e = Embed()
        img = BytesIO()
        # create figure with 2 separate y axes
        fig, ax = plt.subplots()
        ax2 = ax.twinx()

        x_keys = [str(x) for x, _ in distribution]
        rects = ax.bar(
            x_keys, [y for _, y in distribution], color=str(e.color), align="edge"
        )
        ax.bar_label(rects)

        ax.set_xticklabels(x_keys, rotation="vertical")
        ax.set_xlabel(f"Collateral Percent of {'Bonded' if bonded else 'Borrowed'} Eth")

        ax.set_ylim(top=(ax.get_ylim()[1] * 1.1))
        ax.yaxis.set_visible(False)
        ax.get_xaxis().set_major_formatter(
            FuncFormatter(
                lambda n, _: (
                    f"{x_keys[n] if n < len(x_keys) else 0}{'+' if n == len(x_keys) - 1 else ''}%"
                )
            )
        )

        bars = {
            collateral: sum(nodes)
            for collateral, nodes in sorted(data.items(), key=lambda x: x[0])
        }
        line = ax2.plot(x_keys, [bars.get(float(x), 0) for x in x_keys])
        ax2.set_ylim(top=(ax2.get_ylim()[1] * 1.1))
        ax2.tick_params(axis="y", colors=line[0].get_color())
        ax2.get_yaxis().set_major_formatter(
            FuncFormatter(lambda y, _: f"{int(y / 10**3)}k")
        )

        fig.tight_layout()
        ax.legend(rects, ["Node Operators"], loc="upper left")
        ax2.legend(line, ["Staked RPL"], loc="upper right")
        fig.savefig(img, format="png")
        img.seek(0)

        fig.clear()
        plt.close()

        e.title = "RPL Collateral Distribution"
        e.set_image(url="attachment://collateral_distribution.png")
        f = File(img, filename="collateral_distribution.png")
        percentile_strings = [
            f"{x[0]}th percentile: {int(x[1])}% collateral"
            for x in get_percentiles([50, 75, 90, 99], counts)
        ]
        e.description = f"Total Staked RPL: {sum(bars.values()):,.0f}"
        e.set_footer(text="\n".join(percentile_strings))
        await interaction.followup.send(embed=e, files=[f])
        img.close()

    @command()
    @describe(node_address="Node Address or ENS to highlight")
    async def voter_share_distribution(
        self,
        interaction: Interaction,
        node_address: str | None = None,
    ) -> None:
        """
        Show the distribution of RPL staked per borrowed ETH for megapool validators.
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        address, display_name = None, None
        if node_address is not None:
            if "." in node_address:
                display_name = node_address
                address = await ens.resolve_name(node_address)
            elif w3.is_address(address):
                address = w3.to_checksum_address(node_address)
                display_name = address

        log.info(f"{address =}, {display_name = }")

        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "megapool.active_validator_count": {"$gt": 0},
                    "megapool.user_capital": {"$gt": 0},
                    "rpl.megapool_stake": {"$gt": 0},
                }
            },
            {
                "$project": {
                    "address": 1,
                    "rpl_stake": "$rpl.megapool_stake",
                    "borrowed": "$megapool.user_capital",
                    "validators": "$megapool.active_validator_count",
                }
            },
        ]
        results = await (await self.bot.db.node_operators.aggregate(pipeline)).to_list()

        e = Embed()
        e.title = "Megapool RPL per Borrowed ETH"

        if not results:
            e.description = "No data available."
            return await interaction.followup.send(embed=e)

        total_rpl = sum(doc["rpl_stake"] for doc in results)
        total_borrowed = sum(doc["borrowed"] for doc in results)
        avg_ratio = total_rpl / total_borrowed

        ratios = [doc["rpl_stake"] / doc["borrowed"] for doc in results]

        # Cap at the 95th percentile to avoid long tail dominating the histogram
        cap = float(np.percentile(ratios, 95))
        capped_max = max(min(r, cap) for r in ratios)
        possible_step_sizes = [0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100]
        step_size = possible_step_sizes[
            np.argmin([abs(capped_max / 30 - s) for s in possible_step_sizes])
        ]

        buckets: dict[float, list[float]] = {}
        validator_counts: dict[float, int] = {}
        for doc in results:
            ratio = min(doc["rpl_stake"] / doc["borrowed"], cap)
            bucket = step_size * (ratio * 10 // (step_size * 10))
            if bucket not in buckets:
                buckets[bucket] = []
                validator_counts[bucket] = 0
            buckets[bucket].append(doc["rpl_stake"])
            validator_counts[bucket] += doc["validators"]

        distribution = [
            (bucket, validator_counts[bucket]) for bucket in sorted(buckets.keys())
        ]

        counts: list[float] = functools.reduce(
            operator.iadd,
            ([bucket] * num_nodes for bucket, num_nodes in distribution),
            [],
        )

        img = BytesIO()
        fig, ax = plt.subplots()

        # Mark the overall average RPL per borrowed ETH
        avg_pos = avg_ratio / step_size
        ax.axvline(
            avg_pos,
            color="tab:olive",
            linestyle="--",
            zorder=1,
            label=f"Average Stake ({avg_ratio:.1f})",
        )

        leb8_14_breakeven_ratio = avg_ratio / 9
        breakeven_pos = leb8_14_breakeven_ratio / step_size
        ax.axvline(
            breakeven_pos,
            color="tab:red",
            linestyle="--",
            zorder=1,
            label=f"LEB8 14% Breakeven ({leb8_14_breakeven_ratio:.1f})",
        )

        # Highlight target node if provided
        if address is not None:
            target = await self.bot.db.node_operators.find_one(
                {"address": address},
                {"rpl.megapool_stake": 1, "megapool.user_capital": 1},
            )
            if target is not None:
                rpl_stake = (target.get("rpl") or {}).get("megapool_stake", 0)
                borrowed = (target.get("megapool") or {}).get("user_capital", 0)
                target_ratio = (rpl_stake / borrowed) if (borrowed > 0) else 0
                target_pos = min(target_ratio, cap) / step_size
                ax.axvline(
                    target_pos,
                    color="black",
                    linestyle="-",
                    zorder=3,
                    label=f"{display_name} ({target_ratio:.1f})",
                )

        # Match decimal places to step size precision
        decimals = (
            len(f"{step_size:.10f}".rstrip("0").split(".")[1]) if step_size % 1 else 0
        )
        x_keys = [f"{x:.{decimals}f}" for x, _ in distribution]
        rects = ax.bar(
            x_keys, [y for _, y in distribution], color=str(e.color), align="edge"
        )
        ax.bar_label(rects)

        ax.set_xticklabels(x_keys, rotation="vertical")
        ax.set_xlabel("RPL per borrowed ETH")

        ax.set_ylim(top=(ax.get_ylim()[1] * 1.1))
        ax.set_ylabel("Validators")
        ax.get_xaxis().set_major_formatter(
            FuncFormatter(
                lambda n, _: (
                    f"{x_keys[n] if n < len(x_keys) else 0}{'+' if n == len(x_keys) - 1 else ''}"
                )
            )
        )

        fig.tight_layout()
        ax.legend(loc="upper right")
        fig.savefig(img, format="png")
        img.seek(0)

        fig.clear()
        plt.close()

        e.set_image(url="attachment://voter_share_distribution.png")
        f = File(img, filename="voter_share_distribution.png")
        percentile_strings = [
            f"{x[0]}th percentile: {x[1]:.{decimals}f} RPL/ETH"
            for x in get_percentiles([50, 75, 90, 99], counts)
        ]
        e.set_footer(text="\n".join(percentile_strings))
        await interaction.followup.send(embed=e, files=[f])
        img.close()


async def setup(bot):
    await bot.add_cog(Collateral(bot))
