import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Any

import matplotlib
import numpy as np
from aiohttp.client_exceptions import ClientResponseError
from discord import File, Interaction
from discord.app_commands import command, describe
from discord.ext import commands
from discord.utils import as_chunks
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from pymongo import ASCENDING, DESCENDING

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.charts import render_png
from rocketwatch.utils.cronitor_monitor import AsyncMonitor
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.shared_w3 import bacon
from rocketwatch.utils.solidity import beacon_block_to_date, date_to_beacon_block
from rocketwatch.utils.time_debug import timed
from rocketwatch.utils.visibility import is_hidden

cog_id = "proposals"
log = logging.getLogger(f"rocketwatch.{cog_id}")

LOOKUP = {
    "consensus": {
        "N": "Nimbus",
        "P": "Prysm",
        "L": "Lighthouse",
        "T": "Teku",
        "S": "Lodestar",
    },
    "execution": {
        "G": "Geth",
        "B": "Besu",
        "N": "Nethermind",
        "R": "Reth",
        "X": "External",
    },
}

COLORS = {
    "Nimbus": "#CC9133",
    "Prysm": "#40BFBF",
    "Lighthouse": "#9933CC",
    "Teku": "#3357CC",
    "Lodestar": "#FB5B9D",
    "Geth": "#40BFBF",
    "Besu": "#55AA7A",
    "Nethermind": "#2688D9",
    "Reth": "#760910",
    "External": "#808080",
    "Smart Node": "#CC6E33",
    "Allnodes": "#4533cc",
    "No proposals yet": "#E0E0E0",
    "Unknown": "#AAAAAA",
}

PROPOSAL_TEMPLATE = {
    "type": "Unknown",
    "consensus_client": "Unknown",
    "execution_client": "Unknown",
}

# noinspection RegExpUnnecessaryNonCapturingGroup
SMARTNODE_REGEX = re.compile(
    r"^RP(?:(?:-)([A-Z])([A-Z])?)? (?:v)?(\d+\.\d+\.\d+(?:-\w+)?)(?:(?:(?: \()|(?: gw:))(.+)(?:\)))?"
)


def parse_proposal(beacon_block: dict[str, Any]) -> dict[str, Any]:
    graffiti = (
        bytes.fromhex(beacon_block["body"]["graffiti"][2:])
        .decode("utf-8")
        .rstrip("\x00")
    )
    data = {
        "slot": int(beacon_block["slot"]),
        "validator": int(beacon_block["proposer_index"]),
        "graffiti": graffiti,
    } | PROPOSAL_TEMPLATE
    if m := SMARTNODE_REGEX.findall(graffiti):
        groups = m[0]
        # smart node proposal
        data["type"] = "Smart Node"
        data["version"] = groups[2]
        if groups[1]:
            data["consensus_client"] = LOOKUP["consensus"].get(groups[1], "Unknown")
            data["execution_client"] = LOOKUP["execution"].get(groups[0], "Unknown")
        elif groups[0]:
            data["consensus_client"] = LOOKUP["consensus"].get(groups[0], "Unknown")
        if groups[3]:
            data["comment"] = groups[3]
    elif "⚡️Allnodes" in graffiti:
        # Allnodes proposal
        data["type"] = "Allnodes"
        data["consensus_client"] = "Teku"
        data["execution_client"] = "Besu"
    else:
        # normal proposal
        # try to detect the client from the graffiti
        graffiti = graffiti.lower()
        for client in LOOKUP["consensus"].values():
            if client.lower() in graffiti:
                data["consensus_client"] = client
                break
        for client in LOOKUP["execution"].values():
            if client.lower() in graffiti:
                data["execution_client"] = client
                break
    return data


class Proposals(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.monitor = AsyncMonitor("proposals-task")
        self.batch_size = 100
        self.cooldown = timedelta(minutes=5)
        self.bot.loop.create_task(self.loop())

    async def loop(self) -> None:
        await self.bot.wait_until_ready()
        await self.check_indexes()
        while not self.bot.is_closed():
            p_id = time.time()
            await self.monitor.ping(state="run", series=p_id)
            try:
                log.debug("starting proposal task")
                await self.fetch_proposals()
                await self.create_latest_proposal_view()
                log.debug("finished proposal task")
                await self.monitor.ping(state="complete", series=p_id)
            except Exception as err:
                await self.bot.report_error(err)
                await self.monitor.ping(state="fail", series=p_id)
            finally:
                await asyncio.sleep(self.cooldown.total_seconds())

    async def check_indexes(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self.bot.db.proposals.create_index("validator")
            await self.bot.db.proposals.create_index("slot", unique=True)
            await self.bot.db.proposals.create_index(
                [("validator", ASCENDING), ("slot", DESCENDING)]
            )
        except Exception as e:
            log.warning(f"Could not create indexes: {e}")

    async def fetch_proposals(self) -> None:
        if db_entry := (await self.bot.db.last_checked_block.find_one({"_id": cog_id})):
            last_checked_slot = db_entry["slot"]
        else:
            last_checked_slot = 4700012  # last slot before merge

        latest_slot = int(
            (await bacon.get_block_header("finalized"))["data"]["header"]["message"][
                "slot"
            ]
        )
        for slots in as_chunks(
            range(last_checked_slot + 1, latest_slot + 1), self.batch_size
        ):
            log.info(f"Fetching proposals for slots {slots[0]} to {slots[-1]}")
            await asyncio.gather(*[self.fetch_proposal(s) for s in slots])
            await self.bot.db.last_checked_block.replace_one(
                {"_id": cog_id}, {"_id": cog_id, "slot": slots[-1]}, upsert=True
            )

    async def fetch_proposal(self, slot: int) -> None:
        try:
            beacon_header = (await bacon.get_block_header(str(slot)))["data"]["header"][
                "message"
            ]
        except ClientResponseError as e:
            if e.status == HTTPStatus.NOT_FOUND:
                return None
            else:
                raise e

        validator_index = int(beacon_header["proposer_index"])
        query = {"validator_index": validator_index}
        is_megapool = await self.bot.db.minipools.count_documents(query, limit=1)
        is_minipool = await self.bot.db.megapool_validators.count_documents(
            query, limit=1
        )
        if not (is_minipool or is_megapool):
            return None

        beacon_block = (await bacon.get_block(str(slot)))["data"]["message"]
        proposal_data = parse_proposal(beacon_block)
        await self.bot.db.proposals.update_one(
            {"slot": slot}, {"$set": proposal_data}, upsert=True
        )

    async def create_latest_proposal_view(self) -> None:
        log.info("creating latest proposals view")
        pipeline = [
            {
                "$match": {
                    "node_operator": {"$ne": None},
                    "beacon.status": "active_ongoing",
                }
            },
            {
                "$unionWith": {
                    "coll": "minipools",
                    "pipeline": [
                        {
                            "$match": {
                                "node_operator": {"$ne": None},
                                "beacon.status": "active_ongoing",
                            }
                        }
                    ],
                }
            },
            {
                "$lookup": {
                    "from": "proposals",
                    "localField": "validator_index",
                    "foreignField": "validator",
                    "as": "proposals",
                    "pipeline": [{"$sort": {"slot": -1}}, {"$limit": 1}],
                }
            },
            {"$unwind": {"path": "$proposals", "preserveNullAndEmptyArrays": True}},
            {
                "$group": {
                    "_id": "$node_operator",
                    "validator_count": {"$sum": 1},
                    "latest_proposal": {"$first": "$proposals"},
                }
            },
            {"$match": {"latest_proposal": {"$ne": None}}},
            {
                "$project": {
                    "_id": "$_id",
                    "node_operator": "$_id",
                    "validator_count": 1,
                    "latest_proposal": 1,
                }
            },
        ]
        await self.bot.db.latest_proposals.drop()
        await self.bot.db.create_collection(
            "latest_proposals", viewOn="megapool_validators", pipeline=pipeline
        )

    @timed
    async def gather_attribute(
        self, attribute: str, remove_allnodes: bool = False
    ) -> dict[str, Any]:
        pipeline: list[dict[str, Any]] = [
            {
                "$project": {
                    "attribute": f"$latest_proposal.{attribute}",
                    "type": "$latest_proposal.type",
                    "validator_count": 1,
                }
            },
            {
                "$group": {
                    "_id": {"attribute": "$attribute", "type": "$type"},
                    "count": {"$sum": 1},
                    "validator_count": {"$sum": "$validator_count"},
                }
            },
        ]
        distribution = await (
            await self.bot.db.latest_proposals.aggregate(pipeline)
        ).to_list()

        d: dict[str, Any] = {}
        if remove_allnodes:
            # Allnodes counts are tallied here so callers can drop them from totals
            d["remove_from_total"] = {"count": 0, "validator_count": 0}
        for entry in distribution:
            if remove_allnodes and entry["_id"]["type"] == "Allnodes":
                key = "remove_from_total"
            else:
                key = entry["_id"]["attribute"]
            if key in d:
                d[key]["count"] += entry["count"]
                d[key]["validator_count"] += entry["validator_count"]
            else:
                d[key] = entry
        return d

    type Color = str | tuple[float, float, float, float]

    @command()
    @describe(days="how many days to show history for")
    async def version_chart(self, interaction: Interaction, days: int = 90) -> None:
        """
        Show a historical chart of used Smart Node versions
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        window_length = 5

        e = Embed(title="Version Chart")
        e.description = (
            f"The graph below shows proposal stats using a **{window_length}-day rolling window**. "
            f"It relies on proposal frequency to approximate adoption by active validator count."
        )
        # get proposals
        # limit to specified number of days
        proposals = (
            await self.bot.db.proposals.find(
                {
                    "version": {"$exists": 1},
                    "slot": {
                        "$gt": date_to_beacon_block(
                            int((datetime.now() - timedelta(days=days)).timestamp())
                        )
                    },
                }
            )
            .sort("slot", 1)
            .to_list(None)
        )
        max_slot = proposals[-1]["slot"]
        # get versions used after max_slot - window
        start_slot = max_slot - int(5 * 60 * 24 * window_length)
        recent_version_docs = await (
            await self.bot.db.proposals.aggregate(
                [
                    {
                        "$match": {
                            "slot": {"$gte": start_slot},
                            "version": {"$exists": 1},
                        }
                    },
                    {"$group": {"_id": "$version"}},
                    {"$sort": {"_id": -1}},
                ]
            )
        ).to_list()
        recent_versions: list[str] = [v["_id"] for v in recent_version_docs]
        data = {}
        versions = []
        proposal_buffer = []
        tmp_data: dict[str, float] = {}
        for proposal in proposals:
            proposal_buffer.append(proposal)
            if proposal["version"] not in versions:
                versions.append(proposal["version"])
            tmp_data[proposal["version"]] = tmp_data.get(proposal["version"], 0) + 1
            slot = proposal["slot"]
            while proposal_buffer[0]["slot"] < slot - (5 * 60 * 24 * window_length):
                to_remove = proposal_buffer.pop(0)
                tmp_data[to_remove["version"]] -= 1
            date = datetime.fromtimestamp(beacon_block_to_date(slot))
            data[date] = tmp_data.copy()

        # normalize data
        for date, value in data.items():
            total = sum(data[date].values())
            for version in data[date]:
                value[version] /= total

        # stack the data with ax.stackplot
        x = list(data.keys())
        y: dict[str, list[float]] = {v: [] for v in versions}
        for _date, value_ in data.items():
            for version in versions:
                y[version].append(value_.get(version, 0))

        # generate enough distinct colors for all recent versions
        cmap = matplotlib.colormaps["tab20"]
        recent_colors = [
            cmap(i / max(len(recent_versions) - 1, 1))
            for i in range(len(recent_versions))
        ]
        # generate color mapping
        colors: list[Proposals.Color] = ["darkgray"] * len(versions)
        for i, version in enumerate(versions):
            if version in recent_versions:
                colors[i] = recent_colors[recent_versions.index(version)]

        last_slot_data = data[max(x)]
        last_slot_data = {v: last_slot_data[v] for v in recent_versions}
        labels = [
            f"{v} ({last_slot_data[v]:.2%})" if v in recent_versions else "_nolegend_"
            for v in versions
        ]
        # add percentage to labels
        x_arr = np.array(x)

        def draw(fig: Figure) -> None:
            ax = fig.subplots()
            ax.stackplot(x_arr, *y.values(), labels=labels, colors=colors)
            # hide y axis
            ax.tick_params(
                axis="y", which="both", left=False, right=False, labelleft=False
            )
            fig.autofmt_xdate()
            handles, legend_labels = ax.get_legend_handles_labels()
            ax.legend(reversed(handles), reversed(legend_labels), loc="upper left")
            # add a thin line at current time from y=0 to y=1 with a width of 0.5
            ax.plot([x_arr[-1], x_arr[-1]], [0, 1], color="white", alpha=0.25)
            # calculate future point to make latest data more visible
            future_point = x[-1] + timedelta(days=window_length)
            last_y_values = [[yy[-1]] * 2 for yy in y.values()]
            ax.stackplot(
                [x_arr[-1], np.datetime64(future_point)], *last_y_values, colors=colors
            )
            fig.tight_layout()

        img = await render_png(draw, bbox_inches="tight", dpi=300)
        e.set_image(url="attachment://chart.png")

        # send data
        await interaction.followup.send(embed=e, file=File(img, filename="chart.png"))
        img.close()

    async def _count_active_validators(self) -> tuple[int, int]:
        """Validator and node operator totals over the latest_proposals view's population."""
        query = {"node_operator": {"$ne": None}, "beacon.status": "active_ongoing"}
        validators = 0
        node_operators: set[str] = set()
        for collection in (self.bot.db.minipools, self.bot.db.megapool_validators):
            validators += await collection.count_documents(query)
            node_operators.update(await collection.distinct("node_operator", query))
        return validators, len(node_operators)

    async def _distribution_slices(
        self, attr: str, remove_allnodes: bool = False
    ) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
        """Pie slices (label, count) for validators and node operators."""
        # group by client and get count
        data = await self.gather_attribute(attr, remove_allnodes)
        total_validators, total_node_operators = await self._count_active_validators()

        validators = [
            (x, y["validator_count"])
            for x, y in data.items()
            if x != "remove_from_total"
        ]
        validators = sorted(validators, key=lambda x: x[1])

        unobserved_validators = total_validators - sum(d[1] for d in validators)
        if "remove_from_total" in data:
            unobserved_validators -= data["remove_from_total"]["validator_count"]
        validators.insert(0, ("No proposals yet", unobserved_validators))
        # move "Unknown" to be before "No proposals yet"
        validators.insert(
            1,
            validators.pop(
                next(i for i, (x, y) in enumerate(validators) if x == "Unknown")
            ),
        )
        # move "External (if it exists) to be before "Unknown"
        # validators is a list of tuples (name, count)
        if "External" in [x for x, y in validators]:
            validators.insert(
                2,
                validators.pop(
                    next(i for i, (x, y) in enumerate(validators) if x == "External")
                ),
            )

        # get node operators
        node_operators = [
            (x, y["count"]) for x, y in data.items() if x != "remove_from_total"
        ]
        node_operators = sorted(node_operators, key=lambda x: x[1])

        unobserved_node_operators = total_node_operators - sum(
            d[1] for d in node_operators
        )
        if "remove_from_total" in data:
            unobserved_node_operators -= data["remove_from_total"]["count"]
        node_operators.insert(0, ("No proposals yet", unobserved_node_operators))
        # move "Unknown" to be before "No proposals yet"
        node_operators.insert(
            1,
            node_operators.pop(
                next(i for i, (x, y) in enumerate(node_operators) if x == "Unknown")
            ),
        )
        # move "External (if it exists) to be before "Unknown"
        # node_operators is a list of tuples (name, count)
        if "External" in [x for x, y in node_operators]:
            node_operators.insert(
                2,
                node_operators.pop(
                    next(
                        i for i, (x, y) in enumerate(node_operators) if x == "External"
                    )
                ),
            )
        return validators, node_operators

    @staticmethod
    def _plot_distribution(
        ax1: Axes,
        ax2: Axes,
        validators: list[tuple[str, int]],
        node_operators: list[tuple[str, int]],
    ) -> None:
        ax1.pie(
            [x[1] for x in validators],
            colors=[COLORS.get(x[0], "red") for x in validators],
            autopct=lambda pct: (f"{pct:.1f}%") if pct > 5 else "",
            startangle=90,
            textprops={"fontsize": "12"},
        )
        # legend in the top left corner of the plot
        validator_sum = sum(x[1] for x in validators)
        ax1.legend(
            [f"{x[1]} {x[0]} ({x[1] / validator_sum:.2%})" for x in validators],
            loc="lower left",
            bbox_to_anchor=(0, -0.1),
            fontsize=11,
        )
        ax1.set_title("Validators", fontsize=22)

        ax2.pie(
            [x[1] for x in node_operators],
            colors=[COLORS.get(x[0], "red") for x in node_operators],
            autopct=lambda pct: (f"{pct:.1f}%") if pct > 5 else "",
            startangle=90,
            textprops={"fontsize": "12"},
        )
        node_operator_sum = sum(x[1] for x in node_operators)
        ax2.legend(
            [f"{x[1]} {x[0]} ({x[1] / node_operator_sum:.2%})" for x in node_operators],
            loc="lower left",
            bbox_to_anchor=(0, -0.1),
            fontsize=11,
        )
        ax2.set_title("Node Operators", fontsize=22)

    async def proposal_vs_node_operators_embed(
        self, attribute: str, name: str, remove_allnodes: bool = False
    ) -> tuple[Embed, File]:
        title = f"Rocket Pool {name} Distribution {'without Allnodes' if remove_allnodes else ''}"
        validators, node_operators = await self._distribution_slices(
            attribute, remove_allnodes
        )

        def draw(fig: Figure) -> None:
            ax1, ax2 = fig.subplots(1, 2)
            self._plot_distribution(ax1, ax2, validators, node_operators)
            fig.subplots_adjust(left=0, right=1, top=0.9, bottom=0, wspace=0)
            fig.suptitle(title, fontsize=24)

        img = await render_png(draw, figsize=(12, 8))
        e = Embed(title=title)
        e.set_image(url=f"attachment://{attribute}.png")

        # send data
        f = File(img, filename=f"{attribute}.png")
        img.close()
        return e, f

    @command()
    async def client_distribution(
        self, interaction: Interaction, remove_allnodes: bool = False
    ) -> None:
        """
        Generate a distribution graph of clients.
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        embeds, files = [], []
        for attr, name in [
            ["consensus_client", "Consensus Client"],
            ["execution_client", "Execution Client"],
        ]:
            e, f = await self.proposal_vs_node_operators_embed(
                attr, name, remove_allnodes
            )
            embeds.append(e)
            files.append(f)
        await interaction.followup.send(embeds=embeds, files=files)

    @command()
    async def operator_type_distribution(self, interaction: Interaction) -> None:
        """
        Generate a graph of NO groups.
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        embed, file = await self.proposal_vs_node_operators_embed("type", "User")
        await interaction.followup.send(embed=embed, file=file)

    @command()
    async def client_combo_ranking(
        self,
        interaction: Interaction,
        remove_allnodes: bool = False,
        group_by_node_operators: bool = False,
    ) -> None:
        """
        Generate a ranking of most used execution and consensus clients.
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        # aggregate [consensus, execution] pair counts
        client_pairs = await (
            await self.bot.db.latest_proposals.aggregate(
                [
                    {
                        "$match": {
                            "latest_proposal.consensus_client": {"$ne": "Unknown"},
                            "latest_proposal.execution_client": {"$ne": "Unknown"},
                            "latest_proposal.type": {"$ne": "Allnodes"}
                            if remove_allnodes
                            else {"$ne": "deadbeef"},
                        }
                    },
                    {
                        "$group": {
                            "_id": {
                                "consensus": "$latest_proposal.consensus_client",
                                "execution": "$latest_proposal.execution_client",
                            },
                            "count": {
                                "$sum": 1
                                if group_by_node_operators
                                else "$validator_count"
                            },
                        }
                    },
                    {"$sort": {"count": -1}},
                ]
            )
        ).to_list()

        e = Embed(
            title=f"Client Combo Ranking{' without Allnodes' if remove_allnodes else ''}"
        )

        # generate max width of both columns
        max_widths = [
            max(len(x["_id"]["consensus"]) for x in client_pairs),
            max(len(x["_id"]["execution"]) for x in client_pairs),
            max(len(str(x["count"])) for x in client_pairs),
        ]

        desc = "".join(
            f"#{i + 1:<2}\t{pair['_id']['consensus'].rjust(max_widths[0])} & "
            f"{pair['_id']['execution'].ljust(max_widths[1])}\t"
            f"{str(pair['count']).rjust(max_widths[2])}\n"
            for i, pair in enumerate(client_pairs)
        )
        e.description = f"Currently showing {'node operator' if group_by_node_operators else 'validator'} counts\n```{desc}```"
        await interaction.followup.send(embed=e)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(Proposals(bot))
