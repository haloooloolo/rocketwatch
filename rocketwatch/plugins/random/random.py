import logging
import random
from datetime import datetime

import aiohttp
import dice
import humanize
import pytz
from discord import Interaction
from discord.app_commands import Choice, command
from discord.ext import commands
from eth_typing import HexStr
from web3.contract import AsyncContract
from web3.types import TxData

from rocketwatch.bot import RocketWatch
from rocketwatch.utils import solidity
from rocketwatch.utils.block_time import block_to_ts, ts_to_block
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import Embed, el_explorer_url, ens
from rocketwatch.utils.file import TextFile
from rocketwatch.utils.readable import prettify_json_string, pretty_time, s_hex
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.sea_creatures import (
    get_holding_for_address,
    get_sea_creature_for_address,
    sea_creatures,
)
from rocketwatch.utils.shared_w3 import bacon, w3
from rocketwatch.utils.visibility import is_hidden, is_hidden_role_controlled

log = logging.getLogger("rocketwatch.random")


class Random(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.contract_names: list[str] = []

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.contract_names:
            self.contract_names = list(rp.addresses)

    @command()
    async def dice(self, interaction: Interaction, dice_string: str = "1d6") -> None:
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        result = dice.roll(dice_string)
        e = Embed()
        e.title = f"🎲 {dice_string}"
        if len(str(result)) >= 2000:
            e.description = "Result too long to display, attaching as file."
            file = TextFile(str(result), "dice_result.txt")
            await interaction.followup.send(embed=e, file=file)
        else:
            e.description = f"Result: `{result}`"
            await interaction.followup.send(embed=e)

    @command()
    async def burn_reason(self, interaction: Interaction) -> None:
        """Show the largest sources of burned ETH"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        url = "https://ultrasound.money/api/fees/grouped-analysis-1"
        # get data from url using aiohttp
        async with aiohttp.ClientSession() as session, session.get(url) as resp:
            data = await resp.json()

        e = Embed()
        e.set_author(
            name="🔗 Data from ultrasound.money", url="https://ultrasound.money"
        )
        description = "**ETH Burned:**\n```"
        feesburned = data["feesBurned"]
        for span in ["5m", "1h", "24h"]:
            k = f"feesBurned{span}"
            description += f"Last {span}: {solidity.to_float(feesburned[k]):,.2f} ETH ({feesburned[f'{k}Usd']:,.2f} USDC)\n"
        description += "```\n"
        description += "**Burn Ranking (last 5 minutes)**\n"
        ranking = data["leaderboards"]["leaderboard5m"][:5]

        for i, entry in enumerate(ranking):
            # use a number emoji as rank (:one:, :two:, ...)
            # first of convert the number to a word
            description += f":{humanize.apnumber(i + 1)}:"
            if "address" not in entry:
                description += f" {entry['name']}"
            else:
                if not entry["name"]:
                    entry["name"] = s_hex(entry["address"])
                target = f"[{entry['name']}]({cfg.execution_layer.explorer}/address/{entry['address']})"
                description += f" {target}"
            if entry.get("category"):
                description += f" `[{entry['category'].upper()}]`"

            description += "\n\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0"
            description += f"`{solidity.to_float(entry['fees']):,.2f} ETH` :fire:\n"

        e.add_field(
            name="Current Base Fee",
            value=f"`{solidity.to_float(data['latestBlockFees'][0]['baseFeePerGas'], 9):,.2f} GWEI`",
        )
        e.description = description
        await interaction.followup.send(embed=e)

    @command()
    async def dev_time(self, interaction: Interaction) -> None:
        """Timezones too confusing to you? Well worry no more, this command is here to help!"""
        e = Embed()
        time_format = "%A %H:%M:%S %Z"

        dev_time = datetime.now(tz=pytz.timezone("UTC"))
        # seconds since midnight
        midnight = dev_time.replace(hour=0, minute=0, second=0, microsecond=0)
        percentage_of_day = (dev_time - midnight).seconds / (24 * 60 * 60)
        # convert to uint16
        uint_day = int(percentage_of_day * 65535)
        # generate binary string
        binary_day = f"{uint_day:016b}"
        e.add_field(
            name="Coordinated Universal Time",
            value=f"{dev_time.strftime(time_format)}\n"
            f"`{binary_day} (0x{uint_day:04x})`",
        )
        head_slot = int(
            (await bacon.get_block_header("head"))["data"]["header"]["message"]["slot"]
        )
        b = solidity.slot_to_beacon_day_epoch_slot(head_slot)
        e.add_field(name="Beacon Time", value=f"Day {b[0]}, {b[1]}:{b[2]}")

        dev_time = datetime.now(tz=pytz.timezone("Australia/Lindeman"))
        e.add_field(
            name="Most of the core team",
            value=dev_time.strftime(time_format),
            inline=False,
        )

        fornax_time = datetime.now(tz=pytz.timezone("America/Sao_Paulo"))
        e.add_field(
            name="Fornax", value=fornax_time.strftime(time_format), inline=False
        )
        e.add_field(name="Mav", value="Who even knows", inline=False)

        await interaction.response.send_message(embed=e)

    @command()
    async def sea_creatures(
        self, interaction: Interaction, address: str | None = None
    ) -> None:
        """List all sea creatures with their required minimum holding."""
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        e = Embed()
        if address is not None:
            address = address.strip()
            try:
                if address.endswith(".eth"):
                    address = await ens.resolve_name(address)
                if address is None:
                    raise ValueError("unresolved ENS")
                address = w3.to_checksum_address(address)
            except (ValueError, TypeError):
                e.description = "Invalid address"
                await interaction.followup.send(embed=e)
                return
            creature = await get_sea_creature_for_address(address)
            if not creature:
                e.description = f"No sea creature for {address}"
            else:
                # get the required holding from the dictionary
                required_holding = next(
                    h for h, c in sea_creatures.items() if c == creature[0]
                )
                e.add_field(
                    name="Visualization",
                    value=await el_explorer_url(address, prefix=creature),
                    inline=False,
                )
                e.add_field(
                    name="Required holding for emoji",
                    value=f"{required_holding * len(creature)} ETH",
                    inline=False,
                )
                holding = await get_holding_for_address(address)
                e.add_field(
                    name="Actual Holding", value=f"{holding:.0f} ETH", inline=False
                )
        else:
            e.title = "Possible Sea Creatures"
            e.description = ""
            for holding_value, sea_creature in sea_creatures.items():
                e.description += f"{sea_creature}: {holding_value} ETH\n"
        await interaction.followup.send(embed=e)

    @command()
    async def smoothie(self, interaction: Interaction) -> None:
        """Show smoothing pool information"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        e = Embed(title="Smoothing Pool")
        smoothie_eth = solidity.to_float(
            await w3.eth.get_balance(
                await rp.get_address_by_name("rocketSmoothingPool")
            )
        )
        active_statuses = ["active_ongoing", "active_exiting"]
        data = await (
            await self.bot.db.minipools.aggregate(
                [
                    {"$match": {"beacon.status": {"$in": active_statuses}}},
                    {"$project": {"node_operator": 1}},
                    {
                        "$unionWith": {
                            "coll": "megapool_validators",
                            "pipeline": [
                                {"$match": {"beacon.status": {"$in": active_statuses}}},
                                {"$project": {"node_operator": 1}},
                            ],
                        }
                    },
                    {"$group": {"_id": "$node_operator", "count": {"$sum": 1}}},
                    {
                        "$lookup": {
                            "from": "node_operators",
                            "localField": "_id",
                            "foreignField": "address",
                            "as": "meta",
                        }
                    },
                    {"$unwind": {"path": "$meta", "preserveNullAndEmptyArrays": True}},
                    {
                        "$project": {
                            "_id": 1,
                            "count": 1,
                            "smoothie": "$meta.smoothing_pool_registration",
                        }
                    },
                    {
                        "$group": {
                            "_id": "$smoothie",
                            "count": {"$sum": "$count"},
                            "node_count": {"$sum": 1},
                            "counts": {
                                "$addToSet": {"count": "$count", "address": "$_id"}
                            },
                        }
                    },
                    {
                        "$project": {
                            "_id": 1,
                            "count": 1,
                            "node_count": 1,
                            "counts": {
                                "$sortArray": {
                                    "input": "$counts",
                                    "sortBy": {"count": -1},
                                }
                            },
                        }
                    },
                    {
                        "$project": {
                            "_id": 1,
                            "count": 1,
                            "node_count": 1,
                            "counts": {"$slice": ["$counts", 5]},
                        }
                    },
                ]
            )
        ).to_list()
        if not data:
            await interaction.followup.send("No validators found.", ephemeral=True)
            return

        data_by_id = {d["_id"]: d for d in data}
        # node counts
        total_node_count = (
            data_by_id[True]["node_count"] + data_by_id[False]["node_count"]
        )
        smoothie_node_count = data_by_id[True]["node_count"]
        # validator counts
        total_validator_count = data_by_id[True]["count"] + data_by_id[False]["count"]
        smoothie_validator_count = data_by_id[True]["count"]
        d = datetime.now().timestamp() - await rp.call(
            "rocketRewardsPool.getClaimIntervalTimeStart"
        )
        e.description = (
            f"`{smoothie_node_count}/{total_node_count}` nodes (`{smoothie_node_count / total_node_count:.2%}`)"
            f" have joined the smoothing pool.\n"
            f" That is `{smoothie_validator_count}/{total_validator_count}` validators"
            f" (`{smoothie_validator_count / total_validator_count:.2%}`).\n"
            f"The current balance is **`{smoothie_eth:,.2f}` ETH**, {pretty_time(d)} into the reward period.\n\n"
            f"{min(smoothie_node_count, 5)} largest nodes:\n"
        )
        lines = [
            f"- `{d['count']:>4}` validators - {await el_explorer_url(d['address'])}"
            for d in data_by_id[True]["counts"][: min(smoothie_node_count, 5)]
        ]
        e.description += "\n".join(lines)
        await interaction.followup.send(embed=e)

    @command()
    async def odao_challenges(self, interaction: Interaction) -> None:
        """Shows the current oDAO challenges"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        c = await rp.get_contract_by_name("rocketDAONodeTrustedActions")
        # get challenges made
        events = list(
            c.events["ActionChallengeMade"].get_logs(
                from_block=(await w3.eth.get_block("latest")).get("number", 0)
                - 7 * 24 * 60 * 60 // 12
            )
        )
        # remove all events of nodes that aren't challenged anymore
        for event in events:
            if not await rp.call(
                "rocketDAONodeTrusted.getMemberIsChallenged",
                event.args.nodeChallengedAddress,
            ):
                events.remove(event)
        # sort by block number
        events.sort(key=lambda x: x.blockNumber)
        if not events:
            await interaction.followup.send("No active challenges found")
            return
        e = Embed(title="Active oDAO Challenges")
        e.description = ""
        # get duration of challenge period
        challenge_period = await rp.call(
            "rocketDAONodeTrustedSettingsMembers.getChallengeWindow"
        )
        for event in events:
            latest_block = await w3.eth.get_block("latest")
            time_left = challenge_period - (
                latest_block.get("timestamp", 0) - event.args.time
            )
            time_left = pretty_time(time_left)
            challenged = await el_explorer_url(event.args.nodeChallengedAddress)
            challenger = await el_explorer_url(event.args.nodeChallengerAddress)
            e.description += f"**{challenged}** was challenged by **{challenger}**\n"
            e.description += f"Time Left: **{time_left}**\n\n"
        await interaction.followup.send(embed=e)

    @command()
    async def asian_restaurant_name(self, interaction: Interaction) -> None:
        """
        Randomly generated Asian restaurant name
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                "https://www.dotomator.com/api/random_name.json?type=asian"
            ) as resp,
        ):
            a = (await resp.json())["name"]
        await interaction.followup.send(a)

    @command()
    async def mexican_restaurant_name(self, interaction: Interaction) -> None:
        """
        Randomly generated Mexican restaurant name
        """
        prefix = random.choice(
            [
                "El",
                "La",
                "Los",
                "Las",
                "Casa",
                "Don",
                "Doña",
                "Taco",
                "Señor",
                "Mi",
                "Tres",
                "Dos",
                "El Gran",
                "La Casa de",
                "Rancho",
                "Hacienda",
                "Cocina",
                "Pueblo",
                "Villa",
                "Cantina",
            ]
        )
        middle = random.choice(
            [
                "Fuego",
                "Sol",
                "Luna",
                "Loco",
                "Grande",
                "Diablo",
                "Oro",
                "Rojo",
                "Verde",
                "Azteca",
                "Maya",
                "Jalisco",
                "Oaxaca",
                "Baja",
                "Bravo",
                "Charro",
                "Gordo",
                "Amigo",
                "Hermano",
                "Fiesta",
                "Coyote",
                "Tigre",
                "Águila",
                "Toro",
                "Mariposa",
                "Cielo",
                "Sombrero",
                "Guapo",
                "Rico",
                "Caliente",
                "Bonito",
                "Fresco",
            ]
        )
        suffix = random.choice(
            [
                "Cantina",
                "Grill",
                "Kitchen",
                "Cocina",
                "Taqueria",
                "Restaurante",
                "Mexican Grill",
                "Tex-Mex",
                "Cocina & Bar",
                "Street Tacos",
                "Cantina & Grill",
                "Mexican Kitchen",
                "Burrito Bar",
                "",
            ]
        )
        await interaction.response.send_message(f"{prefix} {middle} {suffix}")

    @command()
    async def austrian_restaurant_name(self, interaction: Interaction) -> None:
        """
        Randomly generated Austrian restaurant name
        """
        venues = [
            "Gasthaus",
            "Gasthof",
            "Wirtshaus",
            "Beisl",
            "Stüberl",
            "Heuriger",
            "Landgasthof",
            "Alpengasthof",
            "Berggasthof",
            "Café-Restaurant",
            "Braugasthof",
            "Jausenstation",
        ]
        # (noun, gender): m = masculine, f = feminine, n = neuter
        nouns = [
            ("Adler", "m"),
            ("Hirsch", "m"),
            ("Bär", "m"),
            ("Ochse", "m"),
            ("Löwe", "m"),
            ("Hahn", "m"),
            ("Schwan", "m"),
            ("Fuchs", "m"),
            ("Wolf", "m"),
            ("Steinbock", "m"),
            ("Falke", "m"),
            ("Auerhahn", "m"),
            ("Gamsbock", "m"),
            ("Dachs", "m"),
            ("Lamm", "n"),
            ("Rößl", "n"),
            ("Murmeltier", "n"),
            ("Kreuz", "n"),
            ("Krone", "f"),
            ("Forelle", "f"),
            ("Linde", "f"),
            ("Rose", "f"),
            ("Gams", "f"),
        ]
        adj_stems = [
            "golden",
            "schwarz",
            "weiß",
            "grün",
            "wild",
            "alt",
            "klein",
            "groß",
            "lustig",
            "brav",
            "fein",
            "rot",
        ]
        nom_endings = {"m": "er", "f": "e", "n": "es"}

        noun, gender = random.choice(nouns)
        stem = random.choice(adj_stems)

        # 30% chance for "Zum/Zur" style (dative), otherwise "Venue" style (nominative)
        if random.random() < 0.3:
            article = "Zur" if gender == "f" else "Zum"
            adj = stem.capitalize() + "en"
            name = f"{article} {adj} {noun}"
        else:
            venue = random.choice(venues)
            adj = stem.capitalize() + nom_endings[gender]
            name = f"{venue} {adj} {noun}"

        await interaction.response.send_message(name)

    @command()
    async def get_block_by_timestamp(
        self, interaction: Interaction, timestamp: int
    ) -> None:
        """
        Get a block using its timestamp. Useful for contracts that track block time instead of block number.
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        block = await ts_to_block(timestamp)
        found_ts = await block_to_ts(block)

        if found_ts == timestamp:
            text = f"Found perfect match for timestamp {timestamp}:\nBlock: {block}"
        else:
            text = (
                f"Found close match for timestamp {timestamp}:\n"
                f"Timestamp: {found_ts}\n"
                f"Block: {block}"
            )

        await interaction.followup.send(content=f"```{text}```")

    @command()
    async def get_abi_of_contract(
        self, interaction: Interaction, contract: str
    ) -> None:
        """Retrieve the latest ABI for a contract"""
        await interaction.response.defer(
            ephemeral=is_hidden_role_controlled(interaction)
        )
        try:
            abi = prettify_json_string(await rp.uncached_get_abi_by_name(contract))
            file = TextFile(abi, f"{contract}.{cfg.rocketpool.chain.lower()}.abi.json")
            await interaction.followup.send(file=file)
        except Exception as err:
            await interaction.followup.send(content=f"```Exception: {err!r}```")

    @command()
    async def get_address_of_contract(
        self, interaction: Interaction, contract: str
    ) -> None:
        """Retrieve the latest address for a contract"""
        await interaction.response.defer(
            ephemeral=is_hidden_role_controlled(interaction)
        )
        try:
            address = cfg.rocketpool.manual_addresses.get(contract)
            if not address:
                address = await rp.uncached_get_address_by_name(contract)
            await interaction.followup.send(content=await el_explorer_url(address))
        except Exception as err:
            await interaction.followup.send(content=f"Exception: ```{err!r}```")
            if "No address found for" in repr(err):
                # private response as a tip
                m = (
                    "It may be that you are requesting the address of a contract that does not"
                    " get deployed (e.g. `rocketBase`), is deployed multiple times"
                    " (e.g. `rocketNodeDistributor`),"
                    " or is not yet deployed on the current chain.\n"
                    "... or you messed up the name"
                )
                await interaction.followup.send(content=m)

    @command()
    async def decode_txn(
        self, interaction: Interaction, txn_hash: str, contract_name: str | None = None
    ) -> None:
        """
        Decode transaction calldata
        """
        await interaction.response.defer(
            ephemeral=is_hidden_role_controlled(interaction)
        )
        txn: TxData = await w3.eth.get_transaction(HexStr(txn_hash))
        contract: AsyncContract | None = None
        if contract_name:
            contract = await rp.get_contract_by_name(contract_name)
        elif "to" in txn:
            contract = await rp.get_contract_by_address(txn["to"])
        assert contract is not None
        data = contract.decode_function_input(txn.get("input"))
        await interaction.followup.send(content=f"```Input:\n{data}```")

    # --------- AUTOCOMPLETE --------- #

    @get_address_of_contract.autocomplete("contract")
    @get_abi_of_contract.autocomplete("contract")
    @decode_txn.autocomplete("contract_name")
    async def match_contract_names(
        self, interaction: Interaction, current: str
    ) -> list[Choice[str]]:
        return [
            Choice(name=name, value=name)
            for name in self.contract_names
            if current.lower() in name.lower()
        ][:25]


async def setup(self: RocketWatch) -> None:
    await self.add_cog(Random(self))
