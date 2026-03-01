import logging
from io import BytesIO

import matplotlib.pyplot as plt
from discord import File, Interaction
from discord.ext import commands
from discord.app_commands import command
from pymongo import AsyncMongoClient

from rocketwatch import RocketWatch
from utils import solidity
from utils.cfg import cfg
from utils.embeds import Embed
from utils.rocketpool import rp
from utils.visibility import is_hidden_weak

log = logging.getLogger("rpl")
log.setLevel(cfg["log_level"])


class RPL(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.db = AsyncMongoClient(cfg["mongodb.uri"]).rocketwatch

    @command()
    async def staked_rpl(self, interaction: Interaction):
        """
        Show the amount of RPL staked
        """
        await interaction.response.defer(ephemeral=is_hidden_weak(interaction))
        
        rpl_supply = solidity.to_float(rp.call("rocketTokenRPL.totalSupply"))
        legacy_staked_rpl = solidity.to_float(rp.call("rocketNodeStaking.getTotalLegacyStakedRPL"))
        megapool_staked_rpl = solidity.to_float(rp.call("rocketNodeStaking.getTotalMegapoolStakedRPL"))
        staked_rpl = legacy_staked_rpl + megapool_staked_rpl
        unstaking_rpl = (await (await self.db.node_operators.aggregate([
            {
                '$group': {
                    '_id'                 : 'out',
                    'total_unstaking_rpl_': {
                        '$sum': '$rpl.unstaking'
                    }
                }
            }
        ])).next())['total_unstaking_rpl_']
        unstaked_rpl = rpl_supply - staked_rpl - unstaking_rpl

        def fmt(v):
            if v >= 1_000_000:
                return f"{v / 1_000_000:.2f}M"
            if v >= 1_000:
                return f"{v / 1_000:.1f}K"
            return f"{v:.0f}"

        sizes = [legacy_staked_rpl, megapool_staked_rpl, unstaking_rpl, unstaked_rpl]
        labels = ["Legacy", "Megapools", "Unstaking", "Unstaked"]
        colors = ["#CC4400", "#FF6B00", "#D2B48C", "#808080"]

        total = sum(sizes)
        def autopct(pct):
            return f"{fmt(pct / 100 * total)} ({pct:.1f}%)"

        fig, ax = plt.subplots()
        ax.pie(
            sizes,
            labels=labels,
            colors=colors,
            autopct=autopct,
            startangle=90,
            wedgeprops={"linewidth": 0.5, "edgecolor": "white"},
        )

        img = BytesIO()
        fig.tight_layout()
        fig.savefig(img, format="png")
        img.seek(0)
        plt.close(fig)

        embed = Embed()
        embed.title = "Staked RPL"
        embed.set_image(url="attachment://graph.png")
        file = File(img, filename="graph.png")

        await interaction.followup.send(embed=embed, file=file)
        img.close()

    @command()
    async def withdrawable_rpl(self, interaction: Interaction):
        """
        Show the available liquidity at different RPL/ETH prices
        """
        await interaction.response.defer(ephemeral=is_hidden_weak(interaction))        

        data = await (await self.db.node_operators.aggregate([
            {
                '$match': {
                    'staking_minipool_count': {
                        '$ne': 0
                    }
                }
            }, {
                '$project': {
                    'eth_stake': {
                        '$multiply': [
                            '$effective_node_share', {
                                '$multiply': [
                                    '$staking_minipool_count', 32
                                ]
                            }
                        ]
                    },
                    'rpl_stake': "$rpl.legacy_stake"
                }
            }
        ])).to_list()
        rpl_eth_price = solidity.to_float(rp.call("rocketNetworkPrices.getRPLPrice"))

        # calculate withdrawable RPL at various RPL ETH prices
        # i/10 is the ratio of the price checked to the actual RPL ETH price

        free_rpl_liquidity = {}
        max_collateral = solidity.to_float(rp.call("rocketDAOProtocolSettingsNode.getMinimumLegacyRPLStake"))
        current_withdrawable_rpl = 0
        for i in range(1, 31):

            test_ratio = (i / 10)
            rpl_eth_test_price = rpl_eth_price * test_ratio
            liquid_rpl = 0

            for node in data:
                eth_stake = node["eth_stake"]
                rpl_stake = node["rpl_stake"]

                # if there are no pools, then all the RPL can be withdrawn
                if eth_stake == 0:
                    liquid_rpl += rpl_stake
                    continue

                effective_staked = rpl_stake * rpl_eth_test_price
                collateral_percentage = effective_staked / eth_stake

                # if there is no extra RPL, go to the next node
                if collateral_percentage < max_collateral:
                    continue

                liquid_rpl += ((collateral_percentage - max_collateral) / collateral_percentage) * rpl_stake

            free_rpl_liquidity[i] = (rpl_eth_test_price, liquid_rpl)
            if test_ratio == 1:
                current_withdrawable_rpl = liquid_rpl

        # break the tuples into lists to plot
        x, y = zip(*list(free_rpl_liquidity.values()))

        embed = Embed()
        
        # plot the data
        plt.plot(x, y, color=str(embed.color))
        plt.plot(rpl_eth_price, current_withdrawable_rpl, 'bo')
        plt.xlim(min(x), max(x))

        plt.annotate(f"{rpl_eth_price:.4f}", (rpl_eth_price, current_withdrawable_rpl),
                     textcoords="offset points", xytext=(-10, -5), ha='right')
        plt.annotate(f"{current_withdrawable_rpl / 1000000:.2f} million RPL withdrawable",
                     (rpl_eth_price, current_withdrawable_rpl), textcoords="offset points", xytext=(10, -5),
                     ha='left')
        plt.grid()

        ax = plt.gca()
        ax.set_ylabel("Withdrawable RPL")
        ax.set_xlabel("RPL / ETH ratio")
        ax.yaxis.set_major_formatter(lambda x, _: "{:.1f}m".format(x / 1000000))
        ax.xaxis.set_major_formatter(lambda x, _: "{:.4f}".format(x))

        img = BytesIO()
        plt.tight_layout()
        plt.savefig(img, format='png')
        img.seek(0)

        plt.close()

        embed.title = "Available RPL Liquidity"
        embed.set_image(url="attachment://graph.png")
        f = File(img, filename="graph.png")
        await interaction.followup.send(embed=embed, files=[f])
        img.close()


async def setup(bot):
    await bot.add_cog(RPL(bot))
