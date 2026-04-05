import logging
from io import BytesIO

import numpy as np
import seaborn as sns
from discord import File, Interaction
from discord.app_commands import command
from discord.ext import commands
from matplotlib import pyplot as plt

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.commissions")


class Commissions(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    @command()
    async def commission_history(self, interaction: Interaction) -> None:
        """
        Show the history of minipool commissions.
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        e = Embed(title="Commission History")

        minipools = (
            await self.bot.db.minipools.find().sort("validator_index", 1).to_list(None)
        )
        # create dot chart of minipools
        # x-axis: validator
        # y-axis: node_fee
        ygrid = list(reversed(range(5, 21)))
        step_size = int(len(minipools) / len(ygrid) / 2)

        data: list[list[int]] = [[0] * len(ygrid)]
        for pool in minipools:
            if sum(data[-1]) > step_size:
                # normalize data
                # data[-1] = [x / max(data[-1]) for x in data[-1]]
                data.append([0] * len(ygrid))
            # round to closet ygrid
            data[-1][ygrid.index(int(round(pool["node_fee"] * 100, 0)))] += 1

        # normalize data
        # data[-1] = [x / max(data[-1]) for x in data[-1]]
        # heatmap distribution over time
        data_array = np.array(data).T
        fig, ax = plt.subplots()
        sns.heatmap(
            data_array,
            cmap="viridis",
            yticklabels=list(map(str, ygrid)),
            xticklabels=False,
            ax=ax,
        )
        ax.set_yticklabels(ax.get_yticklabels(), rotation=0, fontsize=8)
        # set y ticks
        ax.set_ylabel("Node Fee")
        fig.tight_layout()

        # respond with image
        img = BytesIO()
        fig.savefig(img, format="png")
        img.seek(0)
        plt.close(fig)
        e.set_image(url="attachment://chart.png")
        e.add_field(name="Total Minipools", value=len(minipools))
        e.add_field(name="Bar Width", value=f"{step_size} minipools")

        # send data
        await interaction.followup.send(
            content="", embed=e, files=[File(img, filename="chart.png")]
        )
        img.close()


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(Commissions(bot))
