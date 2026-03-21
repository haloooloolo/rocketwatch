import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from io import BytesIO

import numpy as np
from aiohttp.client_exceptions import ClientResponseError
from cronitor import Monitor
from discord import File, Interaction
from discord.app_commands import command, describe
from discord.ext import commands
from discord.utils import as_chunks
from matplotlib import pyplot as plt
from pymongo import ASCENDING, DESCENDING

from rocketwatch import RocketWatch
from utils.config import cfg
from utils.embeds import Embed
from utils.shared_w3 import bacon
from utils.solidity import beacon_block_to_date, date_to_beacon_block
from utils.time_debug import timerun_async
from utils.visibility import is_hidden

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


def parse_proposal(beacon_block: dict) -> dict:
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
        self.monitor = Monitor("proposals-task", api_key=cfg.other.secrets.cronitor)
        self.batch_size = 100
        self.cooldown = timedelta(minutes=5)
        self.bot.loop.create_task(self.loop())

    async def loop(self):
        await self.bot.wait_until_ready()
        await self.check_indexes()
        while not self.bot.is_closed():
            p_id = time.time()
            self.monitor.ping(state="run", series=p_id)
            try:
                log.debug("starting proposal task")
                await self.fetch_proposals()
                await self.create_latest_proposal_view()
                log.debug("finished proposal task")
                self.monitor.ping(state="complete", series=p_id)
            except Exception as err:
                await self.bot.report_error(err)
                self.monitor.ping(state="fail", series=p_id)
            finally:
                await asyncio.sleep(self.cooldown.total_seconds())

    async def check_indexes(self):
        await self.bot.wait_until_ready()
        try:
            await self.bot.db.proposals.create_index("validator")
            await self.bot.db.proposals.create_index("slot", unique=True)
            await self.bot.db.proposals.create_index(
                [("validator", ASCENDING), ("slot", DESCENDING)]
            )
        except Exception as e:
            log.warning(f"Could not create indexes: {e}")

    async def fetch_proposals(self):
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
            if e.status == 404:
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

    async def create_latest_proposal_view(self):
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

    @timerun_async
    async def gather_attribute(self, attribute, remove_allnodes=False):
        # Build the match stage to filter out Allnodes if needed
        match_stage: dict = {}
        if remove_allnodes:
            match_stage["$match"] = {"latest_proposal.type": {"$ne": "Allnodes"}}

        pipeline: list[dict] = [
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

        # Add match stage at the beginning if filtering Allnodes
        if remove_allnodes:
            pipeline.insert(0, match_stage)

        distribution = await (
            await self.bot.db.latest_proposals.aggregate(pipeline)
        ).to_list()

        if remove_allnodes:
            d = {"remove_from_total": {"count": 0, "validator_count": 0}}
            for entry in distribution:
                d[entry["_id"]["attribute"]] = entry
            return d
        else:
            # Convert nested _id structure and merge by attribute
            d = {}
            for entry in distribution:
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
    async def version_chart(self, interaction: Interaction, days: int = 90):
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
        recent_versions = await (
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
        recent_versions = [v["_id"] for v in recent_versions]
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

        # use plt.stackplot to stack the data
        x = list(data.keys())
        y: dict[str, list[float]] = {v: [] for v in versions}
        for _date, value_ in data.items():
            for version in versions:
                y[version].append(value_.get(version, 0))

        # generate enough distinct colors for all recent versions
        cmap = plt.colormaps["tab20"]
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
        ax = plt.subplot(111, frameon=False)
        plt.stackplot(x_arr, *y.values(), labels=labels, colors=colors)
        # hide y axis
        plt.tick_params(
            axis="y", which="both", left=False, right=False, labelleft=False
        )
        plt.gcf().autofmt_xdate()
        handles, legend_labels = ax.get_legend_handles_labels()
        ax.legend(reversed(handles), reversed(legend_labels), loc="upper left")
        # add a thin line at current time from y=0 to y=1 with a width of 0.5
        plt.plot([x_arr[-1], x_arr[-1]], [0, 1], color="white", alpha=0.25)
        # calculate future point to make latest data more visible
        future_point = x[-1] + timedelta(days=window_length)
        last_y_values = [[yy[-1]] * 2 for yy in y.values()]
        plt.stackplot(
            [x_arr[-1], np.datetime64(future_point)], *last_y_values, colors=colors
        )
        plt.tight_layout()

        # respond with image
        img = BytesIO()
        plt.savefig(img, format="png", bbox_inches="tight", dpi=300)
        img.seek(0)
        plt.close()
        e.set_image(url="attachment://chart.png")

        # send data
        await interaction.followup.send(embed=e, file=File(img, filename="chart.png"))
        img.close()

    async def plot_axes_with_data(
        self, attr: str, ax1, ax2, remove_allnodes: bool = False
    ):
        # group by client and get count
        data = await self.gather_attribute(attr, remove_allnodes)

        minipools = [
            (x, y["validator_count"])
            for x, y in data.items()
            if x != "remove_from_total"
        ]
        minipools = sorted(minipools, key=lambda x: x[1])

        # get total minipool count from rocketpool
        distinct_ids = await self.bot.db.minipools.find(
            {"beacon.status": "active_ongoing", "status": "staking"}
        ).distinct("_id")
        unobserved_minipools = len(distinct_ids) - sum(d[1] for d in minipools)
        if "remove_from_total" in data:
            unobserved_minipools -= data["remove_from_total"]["validator_count"]
        minipools.insert(0, ("No proposals yet", unobserved_minipools))
        # move "Unknown" to be before "No proposals yet"
        minipools.insert(
            1,
            minipools.pop(
                next(i for i, (x, y) in enumerate(minipools) if x == "Unknown")
            ),
        )
        # move "External (if it exists) to be before "Unknown"
        # minipools is a list of tuples (name, count)
        if "External" in [x for x, y in minipools]:
            minipools.insert(
                2,
                minipools.pop(
                    next(i for i, (x, y) in enumerate(minipools) if x == "External")
                ),
            )

        # get node operators
        node_operators = [
            (x, y["count"]) for x, y in data.items() if x != "remove_from_total"
        ]
        node_operators = sorted(node_operators, key=lambda x: x[1])

        # get total node operator count from rp
        distinct_nos = await self.bot.db.minipools.find(
            {"beacon.status": "active_ongoing", "status": "staking"}
        ).distinct("node_operator")
        unobserved_node_operators = len(distinct_nos) - sum(
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

        # sort data
        ax1.pie(
            [x[1] for x in minipools],
            colors=[COLORS.get(x[0], "red") for x in minipools],
            autopct=lambda pct: (f"{pct:.1f}%") if pct > 5 else "",
            startangle=90,
            textprops={"fontsize": "12"},
        )
        # legend
        total_minipols = sum(x[1] for x in minipools)
        # legend in the top left corner of the plot
        ax1.legend(
            [f"{x[1]} {x[0]} ({x[1] / total_minipols:.2%})" for x in minipools],
            loc="lower left",
            bbox_to_anchor=(0, -0.1),
            fontsize=11,
        )
        ax1.set_title("Minipools", fontsize=22)

        ax2.pie(
            [x[1] for x in node_operators],
            colors=[COLORS.get(x[0], "red") for x in node_operators],
            autopct=lambda pct: (f"{pct:.1f}%") if pct > 5 else "",
            startangle=90,
            textprops={"fontsize": "12"},
        )
        # legend
        total_node_operators = sum(x[1] for x in node_operators)
        ax2.legend(
            [
                f"{x[1]} {x[0]} ({x[1] / total_node_operators:.2%})"
                for x in node_operators
            ],
            loc="lower left",
            bbox_to_anchor=(0, -0.1),
            fontsize=11,
        )
        ax2.set_title("Node Operators", fontsize=22)

    async def proposal_vs_node_operators_embed(
        self, attribute, name, remove_allnodes: bool = False
    ):
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 8))
        # iterate axes in pairs
        title = f"Rocket Pool {name} Distribution {'without Allnodes' if remove_allnodes else ''}"
        await self.plot_axes_with_data(attribute, ax1, ax2, remove_allnodes)

        e = Embed(title=title)

        fig.subplots_adjust(left=0, right=1, top=0.9, bottom=0, wspace=0)
        # set title
        fig.suptitle(title, fontsize=24)

        # respond with image
        img = BytesIO()
        plt.savefig(img, format="png")
        img.seek(0)
        plt.close()
        e.set_image(url=f"attachment://{attribute}.png")

        # send data
        f = File(img, filename=f"{attribute}.png")
        img.close()
        return e, f

    @command()
    async def client_distribution(
        self, interaction: Interaction, remove_allnodes: bool = False
    ):
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
    async def operator_type_distribution(self, interaction: Interaction):
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
    ):
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


async def setup(bot):
    await bot.add_cog(Proposals(bot))
