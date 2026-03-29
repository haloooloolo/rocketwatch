import logging
from io import BytesIO
from typing import Literal

from discord import File, Interaction
from discord.app_commands import command
from discord.ext import commands
from matplotlib import pyplot as plt
from matplotlib.figure import Figure

from rocketwatch import RocketWatch
from utils.embeds import Embed
from utils.readable import render_tree_legacy
from utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.fee_distribution")


class FeeDistribution(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    async def _get_minipools(self, bond: int) -> list[dict]:
        result = await self.bot.db.minipools.aggregate(
            [
                {
                    "$match": {
                        "node_deposit_balance": bond,
                        "beacon.status": "active_ongoing",
                    }
                },
                {
                    "$group": {
                        "_id": {"$round": ["$node_fee", 2]},
                        "count": {"$sum": 1},
                    }
                },
                {"$sort": {"_id": 1}},
            ]
        )
        return await result.to_list()

    async def _get_tree(self) -> dict:
        tree = {}
        for bond in (8, 16):
            subtree = {}
            for entry in await self._get_minipools(bond):
                fee_percentage = entry["_id"] * 100
                subtree[f"{fee_percentage:.0f}%"] = entry["count"]
            tree[f"{bond} ETH"] = subtree
        return tree

    async def _get_pie(self) -> Figure:
        fig, axs = plt.subplots(1, 2)
        for i, bond in enumerate((8, 16)):
            labels = []
            sizes = []

            for entry in await self._get_minipools(bond):
                fee_percentage = entry["_id"] * 100
                labels.append(f"{fee_percentage:.0f}%")
                sizes.append(entry["count"])

            total = sum(sizes)
            # avoid overlapping labels for small slices
            for j in range(len(sizes)):
                if sizes[j] < 0.05 * total:
                    labels[j] = ""

            ax = axs[i]
            ax.set_title(f"{bond} ETH")
            ax.pie(
                sizes,
                labels=labels,
                autopct=lambda p, _total=total: (
                    f"{p * _total / 100:.0f}" if (p >= 5) else ""
                ),
            )
        return fig

    @command()
    async def fee_distribution(
        self, interaction: Interaction, mode: Literal["tree", "pie"] = "pie"
    ) -> None:
        """
        Show the distribution of minipool commission percentages.
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        e = Embed()
        e.title = "Minipool Fee Distribution"

        if mode == "tree":
            tree = await self._get_tree()
            e.description = f"```\n{render_tree_legacy(tree, 'Minipools')}\n```"
            await interaction.followup.send(embed=e)
        elif mode == "pie":
            img = BytesIO()
            fig = await self._get_pie()
            fig.tight_layout()
            fig.savefig(img, format="png")
            img.seek(0)
            fig.clear()
            plt.close()

            file_name = "fee_distribution.png"
            e.set_image(url=f"attachment://{file_name}")
            await interaction.followup.send(embed=e, file=File(img, filename=file_name))


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(FeeDistribution(bot))
