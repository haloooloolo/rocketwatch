import logging
from typing import TypedDict

from discord import Interaction
from discord.app_commands import command
from discord.ext import commands

from rocketwatch import RocketWatch
from utils.embeds import Embed, el_explorer_url
from utils.shared_w3 import bacon
from utils.solidity import BEACON_EPOCH_LENGTH, BEACON_START_DATE
from utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.lottery")


class ValidatorEntry(TypedDict):
    validator: int
    pubkey: str
    node_operator: str


class SyncCommittee(TypedDict):
    start_epoch: int
    end_epoch: int
    validators: list[ValidatorEntry]


class Lottery(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    COMMITTEE_SIZE = 512

    async def get_sync_committee_data(self, period: int) -> SyncCommittee:
        data = (await bacon.get_sync_committee(period * 256))["data"]
        validators = [int(v) for v in data["validators"]]
        projection = {"_id": 0, "validator_index": 1, "pubkey": 1, "node_operator": 1}
        query = {"validator_index": {"$in": validators}}
        minipool_results = await self.bot.db.minipools.find(query, projection).to_list()
        megapool_results = await self.bot.db.megapool_validators.find(
            query, projection
        ).to_list()
        return {
            "start_epoch": period * 256,
            "end_epoch": (period + 1) * 256,
            "validators": [
                {
                    "validator": result["validator_index"],
                    "pubkey": result["pubkey"],
                    "node_operator": result["node_operator"],
                }
                for result in (minipool_results + megapool_results)
                if result.get("node_operator") is not None
            ],
        }

    async def generate_sync_committee_description(self, period: int) -> str:
        data = await self.get_sync_committee_data(period)
        validators = data["validators"]
        perc = len(validators) / Lottery.COMMITTEE_SIZE
        description = f"**Rocket Pool Participation**: {len(validators)}/{Lottery.COMMITTEE_SIZE} ({perc:.2%})\n"
        start_timestamp = BEACON_START_DATE + (
            data["start_epoch"] * BEACON_EPOCH_LENGTH
        )
        description += f"**Start**: Epoch {data['start_epoch']} <t:{start_timestamp}> (<t:{start_timestamp}:R>)\n"
        end_timestamp = BEACON_START_DATE + (data["end_epoch"] * BEACON_EPOCH_LENGTH)
        description += f"**End**: Epoch {data['end_epoch']} <t:{end_timestamp}> (<t:{end_timestamp}:R>)\n"
        validators.sort(key=lambda x: x["validator"])
        description += (
            f"**Validators**: `{', '.join(str(v['validator']) for v in validators)}`\n"
        )
        node_operator_counts: dict[str, int] = {}
        for v in validators:
            if v["node_operator"] not in node_operator_counts:
                node_operator_counts[v["node_operator"]] = 0
            node_operator_counts[v["node_operator"]] += 1
        sorted_operators = sorted(
            node_operator_counts.items(), key=lambda x: x[1], reverse=True
        )
        description += "**Node Operators**: "
        description += ", ".join(
            [
                f"{count}x {await el_explorer_url(node_operator)}"
                for node_operator, count in sorted_operators
            ]
        )
        return description

    @command()
    async def lottery(self, interaction: Interaction) -> None:
        """
        Get the status of the current and next sync committee.
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        header = await bacon.get_block("head")
        current_period = int(header["data"]["message"]["slot"]) // 32 // 256

        embeds = [
            Embed(
                title="Current Sync Committee",
                description=await self.generate_sync_committee_description(
                    current_period
                ),
            ),
            Embed(
                title="Next Sync Committee",
                description=await self.generate_sync_committee_description(
                    current_period + 1
                ),
            ),
        ]
        await interaction.followup.send(embeds=embeds)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(Lottery(bot))
