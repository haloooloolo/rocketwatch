import logging
from datetime import UTC, datetime, timedelta
from io import BytesIO

from bson import SON
from discord import File, Interaction
from discord.app_commands import command
from discord.ext import commands
from matplotlib import pyplot as plt

from rocketwatch import RocketWatch
from utils.embeds import Embed
from utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.metrics")


class Metrics(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.collection = self.bot.db.command_metrics

    @command()
    async def metrics(self, interaction: Interaction):
        """
        Show a summary of event and command statistics
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        try:
            e = Embed(title="Metrics from the last 7 days")
            desc = "```\n"
            # last 7 days
            start = datetime.now(UTC) - timedelta(days=7)

            # get the total number of processed events from the event_queue in the last 7 days
            total_events_processed = await self.bot.db.event_queue.count_documents(
                {"time_seen": {"$gte": start}}
            )
            desc += f"Total Events Processed:\n\t{total_events_processed}\n\n"

            # get the total number of handled commands in the last 7 days
            total_commands_handled = await self.collection.count_documents(
                {"timestamp": {"$gte": start}}
            )
            desc += f"Total Commands Handled:\n\t{total_commands_handled}\n\n"

            # get the average command response time in the last 7 days
            avg_response_time = await (
                await self.collection.aggregate(
                    [
                        {"$match": {"timestamp": {"$gte": start}}},
                        {"$group": {"_id": None, "avg": {"$avg": "$took"}}},
                    ]
                )
            ).to_list(length=1)
            if avg_response_time[0]["avg"] is not None:
                desc += f"Average Command Response Time:\n\t{avg_response_time[0]['avg']:.03} seconds\n\n"

            # get completed rate in the last 7 days
            completed_rate = await (
                await self.collection.aggregate(
                    [
                        {
                            "$match": {
                                "timestamp": {"$gte": start},
                                "status": "completed",
                            }
                        },
                        {"$group": {"_id": None, "count": {"$sum": 1}}},
                    ]
                )
            ).to_list(length=1)
            if completed_rate:
                percent = completed_rate[0]["count"] / (total_commands_handled - 1)
                desc += f"Command Success Rate:\n\t{percent:.03%}\n\n"

            # get the 5 most used commands of the last 7 days
            most_used_commands = await (
                await self.collection.aggregate(
                    [
                        {"$match": {"timestamp": {"$gte": start}}},
                        {"$group": {"_id": "$command", "count": {"$sum": 1}}},
                        {"$sort": {"count": -1}},
                    ]
                )
            ).to_list(length=5)
            desc += "Command Usage:\n"
            for command in most_used_commands:
                desc += f" - {command['_id']}: {command['count']}\n"

            top_users = await (
                await self.collection.aggregate(
                    [
                        {"$match": {"timestamp": {"$gte": start}}},
                        {"$group": {"_id": "$user", "count": {"$sum": 1}}},
                        {"$sort": {"count": -1}},
                    ]
                )
            ).to_list(length=5)
            desc += "\nCommand Count By User:\n"
            for user in top_users:
                desc += f" - {user['_id']['name']}: {user['count']}\n"

            # get the top 5 channels of the last 7 days
            top_channels = await (
                await self.collection.aggregate(
                    [
                        {"$match": {"timestamp": {"$gte": start}}},
                        {"$group": {"_id": "$channel", "count": {"$sum": 1}}},
                        {"$sort": {"count": -1}},
                    ]
                )
            ).to_list(length=5)
            desc += "\nCommand Count By Channel:\n"
            for channel in top_channels:
                desc += f" - {channel['_id']['name']}: {channel['count']}\n"
            e.description = desc + "```"
            await interaction.followup.send(embed=e)
        except Exception as e:
            log.error(f"Failed to get command metrics: {e}")
            await self.bot.report_error(e)

    @command()
    async def metrics_chart(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        # generate mathplotlib chart that shows monthly command usage and monthly event emission, in separate subplots

        command_usage = await (
            await self.collection.aggregate(
                [
                    {
                        "$group": {
                            "_id": {
                                "year": {"$year": "$timestamp"},
                                "month": {"$month": "$timestamp"},
                            },
                            "total": {"$sum": 1},
                        }
                    },
                    {"$sort": SON([("_id.year", 1), ("_id.month", 1)])},
                ]
            )
        ).to_list(None)
        event_emission = await (
            await self.bot.db.event_queue.aggregate(
                [
                    {
                        "$group": {
                            "_id": {
                                "year": {"$year": "$time_seen"},
                                "month": {"$month": "$time_seen"},
                            },
                            "total": {"$sum": 1},
                        }
                    },
                    {"$sort": SON([("_id.year", 1), ("_id.month", 1)])},
                ]
            )
        ).to_list(None)

        # create a new figure
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 10))

        # plot the command usage as bars
        ax1.bar(
            [f"{x['_id']['year']}-{x['_id']['month']:0>2}" for x in command_usage],
            [x["total"] for x in command_usage],
        )
        ax1.set_title("Command Usage")
        ax1.set_xticklabels(
            [f"{x['_id']['year']}-{x['_id']['month']:0>2}" for x in command_usage],
            rotation=45,
        )

        # plot the event usage
        ax2.bar(
            [f"{x['_id']['year']}-{x['_id']['month']:0>2}" for x in event_emission],
            [x["total"] for x in event_emission],
        )
        ax2.set_title("Event Emission")
        ax2.set_xticklabels(
            [f"{x['_id']['year']}-{x['_id']['month']:0>2}" for x in event_emission],
            rotation=45,
        )

        # use minimal whitespace
        fig.tight_layout()

        # store the graph in an file object
        file = BytesIO()
        fig.savefig(file, format="png")
        file.seek(0)

        # clear plot from memory
        plt.close(fig)

        e = Embed(title="Command Usage and Event ")
        e.set_image(url="attachment://metrics.png")
        await interaction.followup.send(
            embed=e, file=File(file, filename="metrics.png")
        )


async def setup(bot):
    await bot.add_cog(Metrics(bot))
