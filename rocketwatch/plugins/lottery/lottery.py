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

    async def get_sync_committee_data(self, period: str) -> SyncCommittee:
        h = await bacon.get_block("head")
        sync_period = int(h["data"]["message"]["slot"]) // 32 // 256
        if period == "next":
            sync_period += 1
        data = (await bacon.get_sync_committee(sync_period * 256))["data"]
        validators = [int(v) for v in data["validators"]]
        projection = {"_id": 0, "validator_index": 1, "pubkey": 1, "node_operator": 1}
        query = {"validator_index": {"$in": validators}}
        minipool_results = await self.bot.db.minipools.find(query, projection).to_list()
        megapool_results = await self.bot.db.megapool_validators.find(
            query, projection
        ).to_list()
        results = minipool_results + megapool_results
        return {
            "start_epoch": sync_period * 256,
            "end_epoch": (sync_period + 1) * 256,
            "validators": [
                {
                    "validator": r["validator_index"],
                    "pubkey": r["pubkey"],
                    "node_operator": r["node_operator"],
                }
                for r in results
                if r.get("node_operator") is not None
            ],
        }

    async def generate_sync_committee_description(self, period: str) -> str:
        data = await self.get_sync_committee_data(period)
        validators = data["validators"]
        perc = len(validators) / 512
        description = (
            f"_Rocket Pool Participation:_ {len(validators)}/512 ({perc:.2%})\n"
        )
        start_timestamp = BEACON_START_DATE + (
            data["start_epoch"] * BEACON_EPOCH_LENGTH
        )
        description += f"_Start:_ Epoch {data['start_epoch']} <t:{start_timestamp}> (<t:{start_timestamp}:R>)\n"
        end_timestamp = BEACON_START_DATE + (data["end_epoch"] * BEACON_EPOCH_LENGTH)
        description += f"_End:_ Epoch {data['end_epoch']} <t:{end_timestamp}> (<t:{end_timestamp}:R>)\n"
        validators.sort(key=lambda x: x["validator"])
        description += (
            f"_Validators:_ `{', '.join(str(v['validator']) for v in validators)}`\n"
        )
        node_operator_counts: dict[str, int] = {}
        for v in validators:
            if v["node_operator"] not in node_operator_counts:
                node_operator_counts[v["node_operator"]] = 0
            node_operator_counts[v["node_operator"]] += 1
        sorted_operators = sorted(
            node_operator_counts.items(), key=lambda x: x[1], reverse=True
        )
        description += "_Node Operators:_ "
        description += ", ".join(
            [
                f"{count}x {await el_explorer_url(node_operator)}"
                for node_operator, count in sorted_operators
            ]
        )
        return description

    @command()
    async def lottery(self, interaction: Interaction):
        """
        Get the status of the current and next sync committee.
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        embeds = [
            Embed(
                title="Current Sync Committee",
                description=await self.generate_sync_committee_description("latest"),
            ),
            Embed(
                title="Next Sync Committee",
                description=await self.generate_sync_committee_description("next"),
            ),
        ]
        await interaction.followup.send(embeds=embeds)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(Lottery(bot))
