import logging
from collections.abc import Mapping
from typing import Any, cast

import aiohttp
import eth_utils
from discord import Color
from eth_typing import BlockNumber

from rocketwatch import RocketWatch
from utils import solidity
from utils.block_time import ts_to_block
from utils.config import cfg
from utils.embeds import Embed, el_explorer_url, format_value
from utils.event import Event, EventPlugin
from utils.readable import cl_explorer_url
from utils.retry import retry
from utils.rocketpool import rp
from utils.sea_creatures import get_sea_creature_for_address
from utils.shared_w3 import bacon, w3
from utils.solidity import beacon_block_to_date, date_to_beacon_block

log = logging.getLogger("rocketwatch.beacon_events")


def _build_finality_embed(
    event_name: str, finality_delay: int, epoch_number: int, timestamp: int
) -> Embed:
    cl_explorer = cfg.consensus_layer.explorer

    if event_name == "finality_delay_event":
        embed = Embed(
            color=Color.from_rgb(235, 86, 86),
            title=":warning: Finality Delay On Beacon Chain",
            description=(
                f"Finality has been delayed for **{finality_delay} Epochs** "
                "on the Beacon Chain!\n\n"
                "Please make sure that your node is operating correctly "
                "to minimize inactivity leak! **Every attestation counts!**"
            ),
        )
        embed.set_image(url="https://c.tenor.com/p3hWK5YRo6IAAAAC/this-is-fine-dog.gif")
    else:
        embed = Embed(
            color=Color.from_rgb(86, 235, 86),
            title=":tada: Finality Recovered",
            description="Finality has been recovered on the Beacon Chain!",
        )

    embed.add_field(
        name="Epoch",
        value=f"[{epoch_number}](https://{cl_explorer}/epoch/{epoch_number})",
    )
    embed.add_field(
        name="Timestamp",
        value=f"<t:{timestamp}:R> (<t:{timestamp}:f>)",
        inline=False,
    )
    return embed


class BeaconEvents(EventPlugin):
    def __init__(self, bot: RocketWatch):
        super().__init__(bot)
        self.finality_delay_threshold = 3

    async def _get_new_events(self) -> list[Event]:
        from_block = BlockNumber(self.last_served_block + 1 - self.lookback_distance)
        return await self.get_past_events(from_block, self._pending_block)

    async def get_past_events(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[Event]:
        from_slot = max(
            0,
            date_to_beacon_block(
                (await w3.eth.get_block(from_block - 1)).get("timestamp", 0)
            )
            + 1,
        )
        to_slot = date_to_beacon_block(
            (await w3.eth.get_block(to_block)).get("timestamp", 0)
        )
        log.info(
            f"Checking for new beacon chain events in slot range [{from_slot}, {to_slot}]"
        )

        events: list[Event] = []
        for slot_number in range(from_slot, to_slot - 1):
            events.extend(
                await self._get_events_for_slot(slot_number, check_finality=False)
            )

        # quite expensive and only really makes sense to check toward the head of the chain
        events.extend(await self._get_events_for_slot(to_slot, check_finality=True))

        log.debug("Finished checking beacon chain events")
        return events

    async def _get_events_for_slot(
        self, slot_number: int, *, check_finality: bool
    ) -> list[Event]:
        try:
            log.debug(f"Checking slot {slot_number}")
            beacon_block = (await bacon.get_block(str(slot_number)))["data"]["message"]
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                log.error(f"Beacon block {slot_number} not found, skipping.")
                return []
            else:
                raise e

        events = await self._get_slashings(beacon_block)
        if proposal_event := await self._get_proposal(beacon_block):
            events.append(proposal_event)

        if check_finality and (
            finality_delay_event := await self._check_finality(beacon_block)
        ):
            events.append(finality_delay_event)

        return events

    async def _get_slashings(self, beacon_block: dict) -> list[Event]:
        slot = int(beacon_block["slot"])
        timestamp = beacon_block_to_date(slot)
        slashings: list[dict[str, str | int]] = []

        for slash in beacon_block["body"]["attester_slashings"]:
            att_1 = set(slash["attestation_1"]["attesting_indices"])
            att_2 = set(slash["attestation_2"]["attesting_indices"])
            slashings.extend(
                {
                    "slashing_type": "Attestation",
                    "validator": index,
                    "slasher": beacon_block["proposer_index"],
                }
                for index in att_1.intersection(att_2)
            )

        slashings.extend(
            {
                "slashing_type": "Proposal",
                "validator": slash["signed_header_1"]["message"]["proposer_index"],
                "slasher": beacon_block["proposer_index"],
            }
            for slash in beacon_block["body"]["proposer_slashings"]
        )

        events: list[Event] = []
        for slash in slashings:
            validator = int(slash["validator"])
            slasher = slash["slasher"]
            minipool: Mapping[str, Any] | None = await self.bot.db.minipools.find_one(
                {"validator_index": validator}
            )
            megapool: (
                Mapping[str, Any] | None
            ) = await self.bot.db.megapool_validators.find_one(
                {"validator_index": validator}
            )
            rp_pool = minipool or megapool
            if rp_pool is None:
                log.info(f"Skipping slashing of unknown validator {validator}")
                continue

            unique_id = (
                f"slash-{validator}"
                f":slasher-{slasher}"
                f":slashing-type-{slash['slashing_type']}"
                f":{timestamp}"
            )
            sea = await get_sea_creature_for_address(
                w3.to_checksum_address(rp_pool["node_operator"])
            )
            node_op_link = await el_explorer_url(rp_pool["node_operator"], prefix=sea)
            validator_link = await cl_explorer_url(validator)
            slasher_link = await cl_explorer_url(slasher)

            embed = Embed(
                title=":rotating_light: Validator Slashed",
                description=f"Validator {validator_link} has been slashed by {slasher_link}",
            )
            embed.set_image(
                url="https://c.tenor.com/p3hWK5YRo6IAAAAC/this-is-fine-dog.gif"
            )
            embed.add_field(name="Node Operator", value=node_op_link)
            embed.add_field(
                name="Reason",
                value=f"`{slash['slashing_type']} Violation`",
            )
            embed.add_field(
                name="Timestamp",
                value=f"<t:{timestamp}:R> (<t:{timestamp}:f>)",
                inline=False,
            )
            events.append(
                Event(
                    topic="beacon_events",
                    embed=embed,
                    event_name="validator_slash_event",
                    unique_id=unique_id,
                    block_number=await ts_to_block(timestamp),
                )
            )

        return events

    @retry(tries=5, delay=10, backoff=2, max_delay=30)
    async def _get_proposal(self, beacon_block: dict) -> Event | None:
        if not (payload := beacon_block["body"].get("execution_payload")):
            # no proposed block
            return None

        if not (api_key := cfg.consensus_layer.beaconcha_secret):
            log.warning("Missing beaconcha.in API key")
            return None

        validator_index = int(beacon_block["proposer_index"])
        minipool: Mapping[str, Any] | None = await self.bot.db.minipools.find_one(
            {"validator_index": validator_index}
        )
        megapool: (
            Mapping[str, Any] | None
        ) = await self.bot.db.megapool_validators.find_one(
            {"validator_index": validator_index}
        )
        rp_pool = minipool or megapool
        if not rp_pool:
            # not proposed by RP validator
            return None

        log.info(f"Validator {validator_index} proposed a block")

        timestamp = int(payload["timestamp"])
        block_number = cast(BlockNumber, int(payload["block_number"]))

        # fetch from beaconcha.in because beacon node is unaware of MEV bribes
        endpoint = f"https://beaconcha.in/api/v1/execution/block/{block_number}"
        async with (
            aiohttp.ClientSession() as session,
            session.get(endpoint, headers={"apikey": api_key}) as resp,
        ):
            response_body = await resp.json()

        log.debug(f"{response_body = }")
        proposal_data = response_body["data"][0]
        log.debug(f"{proposal_data = }")

        block_reward_eth = solidity.to_float(proposal_data["producerReward"])
        log.info(f"Found a proposal with an MEV bribe of {block_reward_eth} ETH")

        if block_reward_eth <= 1:
            # disregard if proposal reward is below 1 ETH
            return None

        if proposal_data["relay"]:
            fee_recipient = proposal_data["relay"]["producerFeeRecipient"]
        else:
            fee_recipient = proposal_data["feeRecipient"]

        sea = await get_sea_creature_for_address(
            w3.to_checksum_address(rp_pool["node_operator"])
        )
        node_op_link = await el_explorer_url(rp_pool["node_operator"], prefix=sea)
        validator_link = await cl_explorer_url(validator_index)
        slot = int(beacon_block["slot"])
        reward_str = format_value(block_reward_eth)
        cl_explorer = cfg.consensus_layer.explorer

        is_smoothie = eth_utils.address.is_same_address(
            fee_recipient, await rp.get_address_by_name("rocketSmoothingPool")
        )

        description = (
            f"Validator {validator_link} has proposed a block "
            f"worth **{reward_str} ETH**!"
        )
        if is_smoothie:
            event_name = "mev_proposal_smoothie_event"
            embed = Embed(
                title=":cup_with_straw: Large Smoothing Pool Proposal",
                description=description,
            )
            embed.set_image(
                url="https://cdn.discordapp.com/attachments/812745786638336021/1106983677130461214/butta-commie-filter.png"
            )
        else:
            event_name = "mev_proposal_event"
            embed = Embed(
                title=":moneybag: Large Minipool Proposal",
                description=description,
            )
        embed.add_field(name="Node Operator", value=node_op_link)
        embed.add_field(
            name="Slot",
            value=f"[{slot}](https://{cl_explorer}/slot/{slot})",
        )

        if is_smoothie:
            smoothie_wei = await w3.eth.get_balance(
                w3.to_checksum_address(fee_recipient),
                block_identifier=block_number,
            )
            smoothie_str = format_value(smoothie_wei / 10**18)
            embed.add_field(
                name="Smoothing Pool Balance",
                value=f"||{smoothie_str}|| ETH",
            )

        embed.add_field(
            name="Timestamp",
            value=f"<t:{timestamp}:R> (<t:{timestamp}:f>)",
            inline=False,
        )

        return Event(
            topic="mev_proposals",
            embed=embed,
            event_name=event_name,
            unique_id=f"mev_proposal:{block_number}:{timestamp}",
            block_number=block_number,
        )

    async def _check_finality(self, beacon_block: dict) -> Event | None:
        slot_number = int(beacon_block["slot"])
        epoch_number = slot_number // 32
        timestamp = beacon_block_to_date(slot_number)

        try:
            # calculate finality delay
            finality_checkpoint = await bacon.get_finality_checkpoint(str(slot_number))
            last_finalized_epoch = int(
                finality_checkpoint["data"]["finalized"]["epoch"]
            )
            finality_delay = epoch_number - last_finalized_epoch
        except aiohttp.ClientResponseError:
            log.exception("Failed to get finality checkpoints")
            return None

        # latest finality delay from db
        delay_entry = await self.bot.db.finality_checkpoints.find_one(
            {"epoch": epoch_number - 1}
        )
        prev_finality_delay = delay_entry["finality_delay"] if delay_entry else 0

        await self.bot.db.finality_checkpoints.update_one(
            {"epoch": epoch_number},
            {"$set": {"finality_delay": finality_delay}},
            upsert=True,
        )

        # if finality delay recovers, notify
        if finality_delay < self.finality_delay_threshold <= prev_finality_delay:
            log.info(
                f"Finality delay recovered from {prev_finality_delay} to {finality_delay}"
            )
            embed = _build_finality_embed(
                "finality_delay_recover_event",
                finality_delay,
                epoch_number,
                timestamp,
            )
            return Event(
                topic="finality",
                embed=embed,
                event_name="finality_delay_recover_event",
                unique_id=f"finality_delay_recover:{epoch_number}",
                block_number=await ts_to_block(timestamp),
            )

        if finality_delay >= max(
            prev_finality_delay + 1, self.finality_delay_threshold
        ):
            log.warning(f"Finality increased to {finality_delay} epochs")
            embed = _build_finality_embed(
                "finality_delay_event",
                finality_delay,
                epoch_number,
                timestamp,
            )
            return Event(
                topic="finality",
                embed=embed,
                event_name="finality_delay_event",
                unique_id=f"{epoch_number}:finality_delay",
                block_number=await ts_to_block(timestamp),
            )

        return None


async def setup(bot):
    await bot.add_cog(BeaconEvents(bot))
