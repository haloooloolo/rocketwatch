import logging
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional

import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter
from discord import File
from discord import Interaction
from discord.app_commands import command
from discord.ext.commands import Cog
from pymongo import AsyncMongoClient, InsertOne

from rocketwatch import RocketWatch
from utils import solidity
from utils.cfg import cfg
from utils.shared_w3 import w3
from utils.rocketpool import rp
from utils.visibility import is_hidden_weak
from utils.block_time import block_to_ts, ts_to_block
from utils.embeds import Embed, el_explorer_url
from utils.event_logs import get_logs


cog_id = "rocksolid"
log = logging.getLogger(cog_id)
log.setLevel(cfg["log_level"])


class RockSolid(Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.client = AsyncMongoClient(cfg["mongodb.uri"])
        self.db = self.client.rocketwatch
        self.deployment_block = 23237366

    async def _fetch_asset_updates(self) -> list[tuple[int, float]]:
        vault_contract = rp.get_contract_by_name("RockSolidVault")

        if db_entry := (await self.db.last_checked_block.find_one({"_id": cog_id})):
            last_checked_block = db_entry["block"]
        else:
            last_checked_block = self.deployment_block

        b_from = last_checked_block + 1
        b_to = w3.eth.get_block_number()
        
        updates = []
        
        async for doc in self.db.rocksolid.find({}):
            updates.append((doc["time"], doc["assets"]))
        
        db_operations = []
        for event_log in get_logs(vault_contract.events.TotalAssetsUpdated, b_from, b_to):
            ts = block_to_ts(event_log.blockNumber)
            assets = solidity.to_float(event_log.args.totalAssets)
            updates.append((ts, assets))
            db_operations.append(InsertOne({"time": ts, "assets": assets}))
        
        async with self.client.start_session() as session:
            async with await session.start_transaction():
                if db_operations:
                    await self.db.rocksolid.bulk_write(db_operations)
                await self.db.last_checked_block.replace_one(
                    {"_id": cog_id},
                    {"_id": cog_id, "block": b_to},
                    upsert=True
                )

        return updates

    @command()
    async def rocksolid(self, interaction: Interaction):
        """
        Summary of RockSolid rETH vault stats.
        """
        await interaction.response.defer(ephemeral=is_hidden_weak(interaction))
        
        current_block = w3.eth.get_block_number()
        now = block_to_ts(current_block)
                
        def get_eth_rate(block_number: int) -> int:
            block_number = max(block_number, self.deployment_block)
            reth_value = rp.call("RockSolidVault.convertToAssets", 10**18, block=block_number)
            return rp.call("rocketTokenRETH.getEthValue", reth_value, block=block_number)
        
        current_eth_rate = get_eth_rate(current_block)
        
        def get_apy(days: int) -> Optional[float]:
            reference_block = ts_to_block(now - timedelta(days=days).total_seconds())
            if reference_block < self.deployment_block:
                return None
            return (current_eth_rate / get_eth_rate(reference_block) - 1) * (365 / days) * 100

        apy_7d = get_apy(days=7)
        apy_30d = get_apy(days=30)
        apy_90d = get_apy(days=90)
                
        tvl_reth = solidity.to_float(rp.call("RockSolidVault.totalAssets"))
        tvl_rock_reth = solidity.to_float(rp.call("RockSolidVault.totalSupply"))
        
        asset_updates: list[tuple[int, float]] = await self._fetch_asset_updates()
        current_date = datetime.fromtimestamp(asset_updates[0][0]).date() - timedelta(days=1)
        current_assets = 0.0

        x, y = [], []
        for ts, assets in asset_updates:
            update_date = datetime.fromtimestamp(ts).date()
            while current_date < update_date:
                x.append(current_date)
                y.append(current_assets)
                current_date += timedelta(days=1)

            current_date = update_date
            current_assets = assets

            x.append(current_date)
            y.append(current_assets)

        fig, ax = plt.subplots(figsize=(6, 2))
        ax.grid()

        ax.plot(x, y, color="#50b1f7")
        ax.xaxis.set_major_formatter(DateFormatter("%b %d"))
        ax.set_ylabel("AUM (rETH)")
        ax.set_xlim((x[0], x[-1]))
        ax.set_ylim((y[0], y[-1] * 1.01))
        
        img = BytesIO()
        fig.tight_layout()
        fig.savefig(img, format='png')
        img.seek(0)
        plt.clf()
        
        ca_reth = rp.get_address_by_name("rocketTokenRETH")
        ca_rock_reth = rp.get_address_by_name("RockSolidVault")
        
        embed = Embed(title="<:rocksolid:1425091714267480158> RockSolid rETH Vault")
        embed.add_field(name="7d APY", value=f"{apy_7d:.2f}%" if apy_7d else "-")
        embed.add_field(name="30d APY", value=f"{apy_30d:.2f}%" if apy_30d else "-")
        embed.add_field(name="90d APY", value=f"{apy_90d:.2f}%" if apy_90d else "-")
        embed.add_field(name="TVL", value=f"`{tvl_reth:,.2f}` {el_explorer_url(ca_reth, name=' rETH')}")
        embed.add_field(name="Supply", value=f"`{tvl_rock_reth:,.2f}` {el_explorer_url(ca_rock_reth, name=' rock.rETH')}")
        embed.set_image(url="attachment://rocksolid-tvl.png")

        await interaction.followup.send(embed=embed, file=File(img, "rocksolid-tvl.png"))


async def setup(bot):
    await bot.add_cog(RockSolid(bot))
