import logging
from io import BytesIO
from typing import Literal

from discord import Interaction, File
from discord.ext import commands
from discord.app_commands import command
from pymongo import AsyncMongoClient
from matplotlib import pyplot as plt

from rocketwatch import RocketWatch
from utils.cfg import cfg
from utils.embeds import Embed
from utils.visibility import is_hidden_weak
from utils.readable import render_tree_legacy

log = logging.getLogger("fee_distribution")
log.setLevel(cfg["log_level"])


class FeeDistribution(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.db = AsyncMongoClient(cfg["mongodb.uri"]).rocketwatch

    @command()
    async def fee_distribution(self, interaction: Interaction, mode: Literal["tree", "pie"]):
        """
        Show the distribution of minipool commission percentages.
        """
        await interaction.response.defer(ephemeral=is_hidden_weak(interaction))

        e = Embed()
        e.title = "Minipool Fee Distribution"
        
        tree = {}
        fig, axs = plt.subplots(1, 2)

        for i, bond in enumerate([8, 16]):            
            result = await self.db.minipools_new.aggregate([
                { 
                    "$match": { 
                        "node_deposit_balance": bond,
                        "beacon.status": "active_ongoing"
                    }
                },
                { 
                    "$group": { 
                        "_id" : { "$round": ["$node_fee", 2] }, 
                        "count": { "$sum": 1 } 
                    }
                }, 
                { 
                    "$sort": { "_id": 1 } 
                }
            ])  
            
            labels = []
            sizes = []
            subtree = {}
            
            for entry in await result.to_list():
                fee_percentage = entry['_id'] * 100
                labels.append(f"{fee_percentage:.0f}%")
                sizes.append(entry["count"])
                subtree[labels[-1]] = sizes[-1]

            ax = axs[i]
            total = sum(sizes)
            tree[f"{bond} ETH"] = subtree
            
            # avoid overlapping labels for small slices
            for i in range(len(sizes)):
                if sizes[i] < 0.05 * total:
                    labels[i] = ""
            
            ax.set_title(f"{bond} ETH")
            ax.pie(sizes, labels=labels, autopct=lambda p: f"{p * total / 100:.0f}" if (p >= 5) else "")

        if mode == "tree":
            e.description = f"```\n{render_tree_legacy(tree, 'Minipools')}\n```"
            await interaction.followup.send(embed=e)
        elif mode == "pie":
            img = BytesIO()
            fig.tight_layout()
            fig.savefig(img, format='png')
            img.seek(0)
            fig.clear()
            plt.close()

            file_name = "fee_distribution.png"
            e.set_image(url=f"attachment://{file_name}")
            await interaction.followup.send(embed=e, file=File(img, filename=file_name))



async def setup(bot):
    await bot.add_cog(FeeDistribution(bot))
