from __future__ import annotations

import logging
import warnings
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar, NotRequired, TypedDict

import humanize
from discord import Color
from eth_typing import BlockNumber, ChecksumAddress, HexStr
from web3.constants import ADDRESS_ZERO
from web3.types import EventData, TxReceipt

from rocketwatch.utils import solidity
from rocketwatch.utils.block_time import block_to_ts
from rocketwatch.utils.dao import (
    DefaultDAO,
    ProtocolDAO,
    build_claimer_description,
    decode_setting_multi,
)
from rocketwatch.utils.embeds import (
    Embed,
    build_event_embed,
    build_rich_event_embed,
    build_small_event_embed,
    el_explorer_url,
    format_value,
)
from rocketwatch.utils.readable import cl_explorer_url, s_hex
from rocketwatch.utils.rocketpool import ValidatorInfo, rp
from rocketwatch.utils.shared_w3 import bacon, w3
from rocketwatch.utils.solidity import SUBMISSION_KEYS
from rocketwatch.utils.type_markers import (
    ContractAddress,
    MegapoolAddress,
    MinipoolAddress,
    NodeAddress,
    Percentage,
    WalletAddress,
    Wei,
    _addr,
    auto_format,
)

log = logging.getLogger("rocketwatch.events")

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------


class LogEventData(EventData, total=False):
    """Event wrapper: all of :class:`EventData` plus dynamically added keys."""

    topics: list[str]
    assignmentCount: int
    amountOfStETH: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inline_sender(fmt: dict[str, Any], raw: Mapping[str, Any]) -> str:
    """Build the fancy sender string from pre-formatted *fmt*.

    Compares raw addresses to decide whether caller differs from sender.
    """
    if "caller" in raw and raw["caller"] != raw["from"]:
        return f"{fmt['caller']} ({fmt['from']})"
    return str(fmt["from"])


def _get_proposal_id(args: Mapping[str, Any]) -> int:
    return int(args.get("proposalID") or args["proposalId"])


# ---------------------------------------------------------------------------
# TypedDicts
# ---------------------------------------------------------------------------


class LogEventContext(TypedDict):
    """Fields injected into every args dict by ``process_events``."""

    transactionHash: HexStr
    blockNumber: BlockNumber
    event_name: str


class MinipoolEventContext(LogEventContext):
    """Fields injected by global-event preprocessing for minipool events."""

    minipool: MinipoolAddress
    pubkey: NotRequired[HexStr]


class MegapoolEventContext(LogEventContext):
    """Fields injected by global-event preprocessing for megapool events."""

    megapool: MegapoolAddress
    node: NodeAddress


# Functional TypedDict bases for events that access args["from"].

_FromNodeField = TypedDict("_FromNodeField", {"from": NodeAddress})


class FromNodeContext(_FromNodeField, LogEventContext):
    """LogEventContext + ``from`` as auto-formatted NodeAddress."""


_FromNodeCallerField = TypedDict(
    "_FromNodeCallerField",
    {"from": NodeAddress, "caller": NotRequired[NodeAddress]},
)


class FromNodeCallerContext(_FromNodeCallerField, LogEventContext):
    """For events that display both ``from`` and ``caller`` as formatted links."""


_FromCallerField = TypedDict(
    "_FromCallerField",
    {"from": NotRequired[ChecksumAddress], "caller": NotRequired[ChecksumAddress]},
)


class FromCallerContext(_FromCallerField, LogEventContext):
    """LogEventContext + optional raw ``from``/``caller``."""


class MinipoolFromCallerContext(_FromCallerField, MinipoolEventContext):
    """MinipoolEventContext + optional raw ``from``/``caller``."""


class MegapoolFromCallerContext(_FromCallerField, MegapoolEventContext):
    """MegapoolEventContext + optional raw ``from``/``caller``."""


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class LogEvent(ABC):
    """Base class for log event types.

    Each subclass builds its own Discord embed(s) explicitly — no template
    lookup, no auto-transformation.  Return ``[]`` from ``build_embeds`` to
    filter the event out entirely.

    Subclasses should define a nested ``Args`` TypedDict to declare the
    expected fields and their formatting markers.
    """

    event_name: str
    is_global: ClassVar[bool] = False

    class Args(LogEventContext):
        """Default args type — override in subclasses."""

    async def resolve(
        self,
        args: dict[str, Any],
        event: LogEventData,
    ) -> LogEvent | None:
        """Override to dispatch to a different event class.

        Return ``None`` to filter out the event entirely.
        """
        return self

    async def _fmt(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Auto-format *args* using this class's nested ``Args`` TypedDict."""
        return dict(await auto_format(args, type(self).Args))

    @abstractmethod
    async def build_embeds(
        self,
        args: Any,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]: ...


# ===================================================================
# Group 1: Network / Protocol Events
# ===================================================================


class NegativeRETHRatioEvent(LogEvent):
    event_name = "negative_rETH_ratio_update_event"

    class Args(LogEventContext):
        totalEth: int
        rethSupply: int

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        reth_supply = solidity.to_float(args["rethSupply"])
        total_eth = solidity.to_float(args["totalEth"])
        curr_rate = total_eth / reth_supply if reth_supply > 0 else 1.0
        prev_rate = solidity.to_float(
            await rp.call(
                "rocketTokenRETH.getExchangeRate", block=args["blockNumber"] - 1
            )
        )
        d = curr_rate - prev_rate
        if d > 0 or abs(d) < 0.00001:
            return []
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":warning: Negative rETH Ratio Update",
                description=(
                    f"The rETH ratio has decreased from "
                    f"`{format_value(prev_rate)}` to `{format_value(curr_rate)}`!"
                ),
            )
        ]


class PriceUpdateEvent(LogEvent):
    event_name = "price_update_event"

    class Args(LogEventContext):
        rplPrice: int

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        block = args["blockNumber"]
        period_start = await rp.call(
            "rocketRewardsPool.getClaimIntervalTimeStart", block=block
        )
        period_length = await rp.call(
            "rocketRewardsPool.getClaimIntervalTime", block=block
        )
        reward_period_end = period_start + period_length
        update_rate = await rp.call(
            "rocketDAOProtocolSettingsNetwork.getSubmitPricesFrequency", block=block
        )
        ts = await block_to_ts(block)
        if not (ts < reward_period_end < ts + update_rate):
            return []

        value = format_value(solidity.to_float(args["rplPrice"]))
        embed = await build_event_embed(
            tx_hash=args["transactionHash"],
            block_number=args["blockNumber"],
            title=":moneybag: RPL Price Update",
            description=(
                f"The RPL price has been updated to **`{value} RPL/ETH`!**\n\n"
                f"**This will be the last update before the current reward period ends!**\n"
                f"You have until <t:{reward_period_end}> (<t:{reward_period_end}:R>) "
                f"to increase your RPL stake!"
            ),
        )
        embed.colour = Color.from_rgb(86, 235, 235)
        return [embed]


# ===================================================================
# Group 2: Token Events
# ===================================================================


class TransferEvent(LogEvent):
    """Handles rETH Transfer events (whale transfers >= 1000 rETH)."""

    event_name = "reth_transfer_event"

    class Args(FromNodeContext):
        to: WalletAddress
        value: int

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        amount = args["value"] / 10**18

        if amount < 1000:
            return []

        fmt = await self._fmt(args)
        return [
            await build_rich_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                receipt=receipt,
                sender=args["from"],
                caller=receipt["from"],
                title=":whale: Large rETH Transfer",
                description=(
                    f"**{format_value(amount)} rETH** transferred "
                    f"from {fmt['from']} to {fmt['to']}!"
                ),
            )
        ]


class RETHBurnEvent(LogEvent):
    event_name = "reth_burn_event"

    class Args(FromNodeCallerContext):
        amount: Wei
        ethAmount: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount = fmt["amount"]
        if amount < 1:
            return []
        amount_s = format_value(amount)
        eth_s = format_value(fmt["ethAmount"])

        if amount < 100:
            sender = _inline_sender(fmt, args)
            return [
                await build_small_event_embed(
                    f":fire: {sender} burned **{amount_s} rETH** for {eth_s} ETH!",
                    args["transactionHash"],
                )
            ]
        return [
            await build_rich_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                receipt=receipt,
                sender=args["from"],
                caller=receipt["from"],
                title=":fire: rETH Burn",
                description=f"Burned **{amount_s} rETH** for {eth_s} ETH!",
            )
        ]


class RPLInflationEvent(LogEvent):
    event_name = "rpl_inflation_event"

    class Args(LogEventContext):
        value: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        value = format_value(fmt["value"])
        total_supply = int(
            solidity.to_float(await rp.call("rocketTokenRPL.totalSupply"))
        )
        inflation = round(await rp.get_annual_rpl_inflation() * 100, 4)
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":chart_with_upwards_trend: RPL Inflation Occurred",
                description=(
                    f"{value} new RPL minted! "
                    f"The new total supply is {humanize.intcomma(total_supply)} RPL."
                ),
                fields=[("Current Inflation", f"{inflation}%", False)],
            )
        ]


class RPLMigrationEvent(LogEvent):
    event_name = "rpl_migration_event"

    class Args(FromNodeContext):
        amount: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount = fmt["amount"]
        amount_s = format_value(amount)
        if amount < 1000:
            return [
                await build_small_event_embed(
                    f":arrows_counterclockwise: {fmt['from']} migrated **{amount_s} RPL**!",
                    args["transactionHash"],
                )
            ]
        return [
            await build_rich_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                receipt=receipt,
                sender=args["from"],
                caller=receipt["from"],
                title=":arrows_counterclockwise: RPL Migration",
                description=(
                    f"{fmt['from']} migrated **{amount_s} RPL v1** to the new token contract!"
                ),
            )
        ]


class RPLStakeEvent(LogEvent):
    event_name = "rpl_stake_event"

    class Args(FromNodeCallerContext):
        amount: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        rpl_ratio = solidity.to_float(await rp.call("rocketNetworkPrices.getRPLPrice"))
        fmt = await self._fmt(args)
        amount = fmt["amount"]
        eth_amount = amount * rpl_ratio

        if amount < 1000:
            return []

        amount_s = format_value(amount)
        eth_s = format_value(eth_amount)
        threshold = (3 * 2.4) / rpl_ratio

        if amount < threshold:
            fancy = _inline_sender(fmt, args)
            return [
                await build_small_event_embed(
                    f":moneybag: {fancy} staked "
                    f"**{amount_s} RPL** (worth {eth_s} ETH)!",
                    args["transactionHash"],
                )
            ]
        return [
            await build_rich_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                receipt=receipt,
                sender=args["from"],
                caller=receipt["from"],
                title=":moneybag: RPL Stake",
                description=(
                    f"{fmt['from']} staked **{amount_s} RPL** (worth {eth_s} ETH)!"
                ),
            )
        ]


class RPLWithdrawEvent(LogEvent):
    event_name = "rpl_withdraw_event"

    class Args(LogEventContext):
        amount: Wei
        to: WalletAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        rpl_ratio = solidity.to_float(await rp.call("rocketNetworkPrices.getRPLPrice"))
        amount = fmt["amount"]
        eth_amount = amount * rpl_ratio

        if eth_amount < 16:
            return []

        amount_s = format_value(amount)
        eth_s = format_value(eth_amount)
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":leaves: RPL Withdrawal",
                description=(
                    f"{fmt['to']} withdrew **{amount_s} RPL** (worth {eth_s} ETH)!"
                ),
            )
        ]


class NodeRPLSlashEvent(LogEvent):
    event_name = "node_rpl_slash_event"

    class Args(LogEventContext):
        amount: Wei
        ethValue: Wei
        node: NodeAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount_s = format_value(fmt["amount"])
        eth_s = format_value(fmt["ethValue"])
        embed = await build_event_embed(
            tx_hash=args["transactionHash"],
            block_number=args["blockNumber"],
            title=":rotating_light: Node Operator Slashed",
            description=(
                f"Node operator {fmt['node']} has been slashed "
                f"for **{amount_s} RPL** ({eth_s} ETH)!"
            ),
        )
        embed.set_image(url="https://c.tenor.com/p3hWK5YRo6IAAAAC/this-is-fine-dog.gif")
        return [embed]


# ===================================================================
# Group 3: Deposit Pool / Validator Events
# ===================================================================


class PoolDepositEvent(LogEvent):
    event_name = "pool_deposit_event"

    class Args(FromNodeCallerContext):
        amount: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount = fmt["amount"]
        amount_s = format_value(amount)
        if amount < 100:
            fancy = _inline_sender(fmt, args)
            return [
                await build_small_event_embed(
                    f":rocket: {fancy} deposited **{amount_s} ETH** for rETH!",
                    args["transactionHash"],
                )
            ]
        embed = await build_rich_event_embed(
            tx_hash=args["transactionHash"],
            block_number=args["blockNumber"],
            receipt=receipt,
            sender=args["from"],
            caller=receipt["from"],
            title=":rocket: Pool Deposit",
            description=f"**{amount_s} ETH** deposited for rETH!",
        )
        if amount >= 1000:
            embed.set_image(
                url="https://media.giphy.com/media/VIX2atZr8dCKk5jF6L/giphy.gif"
            )
        return [embed]


class PoolDepositAssignedEvent(LogEvent):
    event_name = "pool_deposit_assigned_event"

    class Args(LogEventContext):
        minipool: MinipoolAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        count = event.get("assignmentCount", 0)
        if count == 0:
            return []

        # look up node operator
        contract = await rp.assemble_contract("rocketMinipool", args["minipool"])
        node = await contract.functions.getNodeAddress().call()
        node_link = await _addr(node)

        if count == 1:
            minipool_link = await _addr(args["minipool"])
            args["event_name"] = "pool_deposit_assigned_single_event"
            return [
                await build_small_event_embed(
                    f":handshake: Minipool {minipool_link} owned by operator "
                    f"{node_link} has been matched and left the queue!",
                    args["transactionHash"],
                )
            ]

        return [
            await build_small_event_embed(
                f":handshake: {count} minipools have been matched and left the queue!",
                args["transactionHash"],
            )
        ]


class PoolDepositRecycledEvent(LogEvent):
    event_name = "pool_deposit_recycled_event"

    class Args(LogEventContext):
        amount: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount_s = format_value(fmt["amount"])
        return [
            await build_small_event_embed(
                f":recycle: A protocol contract deposited "
                f"**{amount_s} ETH** into the deposit pool!",
                args["transactionHash"],
            )
        ]


class ValidatorQueueExitedEvent(LogEvent):
    event_name = "validator_queue_exited_event"

    class Args(LogEventContext):
        nodeAddress: NodeAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)

        # Determine queue type by finding the MegapoolValidatorDequeued event
        # in the same receipt and checking the validator's expressUsed field
        # at the previous block (before dequeue resets it).
        queue_type = ""
        try:
            megapool_address = await rp.call(
                "rocketNodeManager.getMegapoolAddress", args["nodeAddress"]
            )
            if megapool_address != ADDRESS_ZERO:
                mp = await rp.get_contract_by_name("rocketMegapoolDelegate")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    dequeued_logs = (
                        mp.events.MegapoolValidatorDequeued().process_receipt(receipt)
                    )
                if dequeued_logs:
                    validator_id = dequeued_logs[0].args["validatorId"]
                    info = await rp.call(
                        "rocketMegapoolDelegate.getValidatorInfo",
                        validator_id,
                        address=megapool_address,
                        block=args["blockNumber"] - 1,
                    )
                    queue_type = (
                        " express" if ValidatorInfo(*info).express_used else " standard"
                    )
        except Exception:
            log.exception("Failed to determine queue type for QueueExited event")

        return [
            await build_small_event_embed(
                f":leaves: {fmt['nodeAddress']} has removed a validator from the{queue_type} queue!",
                args["transactionHash"],
            )
        ]


class ETHDepositEvent(LogEvent):
    event_name = "eth_deposit_event"

    class Args(FromNodeContext):
        nodeAddress: NodeAddress
        amount: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount = fmt["amount"]
        amount_s = format_value(amount)
        if amount < 32:
            return [
                await build_small_event_embed(
                    f":moneybag: {fmt['from']} deposited "
                    f"**{amount_s} ETH** into node {fmt['nodeAddress']}!",
                    args["transactionHash"],
                )
            ]
        return [
            await build_rich_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                receipt=receipt,
                sender=args["from"],
                caller=receipt["from"],
                title=":moneybag: Node ETH Deposit",
                description=(
                    f"{fmt['from']} deposited **{amount_s} ETH** into node {fmt['nodeAddress']}!"
                ),
            )
        ]


class ETHWithdrawEvent(LogEvent):
    event_name = "eth_withdraw_event"

    class Args(LogEventContext):
        amount: Wei
        to: WalletAddress
        nodeAddress: NodeAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount = fmt["amount"]
        amount_s = format_value(amount)
        if amount < 100:
            return [
                await build_small_event_embed(
                    f":leaves: {fmt['to']} withdrew "
                    f"**{amount_s} ETH** from node {fmt['nodeAddress']}!",
                    args["transactionHash"],
                )
            ]
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":leaves: Node ETH Withdrawal",
                description=(
                    f"{fmt['to']} withdrew **{amount_s} ETH** from node {fmt['nodeAddress']}!"
                ),
            )
        ]


class CreditWithdrawnEvent(LogEvent):
    event_name = "credit_withdrawn_event"

    class Args(LogEventContext):
        nodeAddress: NodeAddress
        amount: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount = fmt["amount"]
        if amount < 32:
            return [
                await build_small_event_embed(
                    f":leaves: {fmt['nodeAddress']} withdrew "
                    f"**{format_value(amount)} ETH** of credit!",
                    args["transactionHash"],
                )
            ]
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":leaves: Credit Withdrawal",
                description=(
                    f"{fmt['nodeAddress']} withdrew **{format_value(amount)} ETH** of credit!"
                ),
            )
        ]


class ValidatorDepositEvent(LogEvent):
    event_name = "validator_deposit_event"

    class Args(FromNodeContext):
        amount: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount_s = format_value(fmt["amount"])
        return [
            await build_small_event_embed(
                f":construction_site: {fmt['from']} created a validator "
                f"with a **{amount_s} ETH** bond!",
                args["transactionHash"],
            )
        ]


class ValidatorMultiDepositEvent(LogEvent):
    event_name = "validator_multi_deposit_event"

    class Args(FromNodeContext):
        numberOfValidators: int
        totalBond: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        num = args["numberOfValidators"]
        amount_s = format_value(fmt["totalBond"])

        if num == 1:
            args["event_name"] = "validator_deposit_event"
            return [
                await build_small_event_embed(
                    f":construction_site: {fmt['from']} created a validator "
                    f"with a **{amount_s} ETH** bond!",
                    args["transactionHash"],
                )
            ]

        if num >= 5:
            return [
                await build_rich_event_embed(
                    tx_hash=args["transactionHash"],
                    block_number=args["blockNumber"],
                    receipt=receipt,
                    sender=args["from"],
                    caller=receipt["from"],
                    title=":construction_site: Multi Validator Deposit",
                    description=(
                        f"**{num} validators** created with a total bond of "
                        f"**{amount_s} ETH**!"
                    ),
                )
            ]

        return [
            await build_small_event_embed(
                f":construction_site: {fmt['from']} created "
                f"**{num} validators** with a **{amount_s} ETH** bond!",
                args["transactionHash"],
            )
        ]


class NodeMerkleRewardsClaimedEvent(LogEvent):
    event_name = "node_merkle_rewards_claimed"

    async def build_embeds(
        self, args: Any, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        return []  # TODO


# ===================================================================
# Group 4: Auction Events
# ===================================================================


class AuctionLotCreateEvent(LogEvent):
    event_name = "auction_lot_create_event"

    class Args(LogEventContext):
        by: NodeAddress
        rplAmount: Wei
        lotIndex: int

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        rpl_s = format_value(fmt["rplAmount"])
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":scales: Lot Created",
                description=(
                    f"{fmt['by']} created Lot #{args['lotIndex']}, "
                    f"which will auction off {rpl_s} RPL!"
                ),
            )
        ]


class AuctionBidEvent(LogEvent):
    event_name = "auction_bid_event"

    class Args(LogEventContext):
        bidAmount: Wei
        lotIndex: int
        by: NodeAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        eth = fmt["bidAmount"]
        price = solidity.to_float(
            await rp.call(
                "rocketAuctionManager.getLotPriceAtBlock",
                args["lotIndex"],
                args["blockNumber"],
            )
        )
        rpl_amount = eth / price
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":scales: Bid On Lot",
                description=(
                    f"{fmt['by']} bid {format_value(eth)} ETH for "
                    f"{format_value(rpl_amount)} RPL on Lot #{args['lotIndex']}!"
                ),
            )
        ]


class AuctionRPLRecoverEvent(LogEvent):
    event_name = "auction_rpl_recover_event"

    class Args(LogEventContext):
        rplAmount: Wei
        lotIndex: int

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        rpl_s = format_value(fmt["rplAmount"])
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":scales: RPL Recovered From Lot",
                description=(f"{rpl_s} RPL recovered from Lot #{args['lotIndex']}!"),
            )
        ]


# ===================================================================
# Group 5: Bootstrap Events (rocketDAOProtocol)
# ===================================================================


class BootstrapPDAOSettingEvent(LogEvent):
    event_name = "bootstrap_pdao_setting_event"

    class Args(LogEventContext):
        settingPath: str
        value: Any

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        value = args["value"]
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":satellite_orbital: pDAO Bootstrap Mode: Setting Modified",
                description=(f"Setting `{args['settingPath']}` set to `{value}`!"),
            )
        ]


class BootstrapPDAOSettingMultiEvent(LogEvent):
    event_name = "bootstrap_pdao_setting_multi_event"

    class Args(LogEventContext):
        settingContractNames: list[str]
        settingPaths: list[str]
        types: list[int]
        values: list[bytes]

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        description = decode_setting_multi(dict(args), args["values"])
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":satellite_orbital: pDAO Bootstrap Mode: Multiple Settings Modified",
                description=description,
            )
        ]


class BootstrapPDAOClaimerEvent(LogEvent):
    event_name = "bootstrap_pdao_claimer_event"

    class Args(LogEventContext):
        nodePercent: int
        protocolPercent: int
        trustedNodePercent: int

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":satellite_orbital: pDAO Bootstrap Mode: Changed Reward Distribution",
                description=f"```{build_claimer_description(args)}```",
            )
        ]


class BootstrapPDAOSpendTreasuryEvent(LogEvent):
    event_name = "bootstrap_pdao_spend_treasury_event"

    class Args(LogEventContext):
        amount: Wei
        recipientAddress: WalletAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount_s = format_value(fmt["amount"])
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":satellite_orbital: pDAO Bootstrap Mode: Treasury Spend",
                description=f"**{amount_s} RPL** from treasury sent to {fmt['recipientAddress']}!",
            )
        ]


class _BootstrapPDAOTreasuryRecurringEvent(LogEvent):
    """Base for new/update recurring treasury spend events."""

    class Args(LogEventContext):
        amountPerPeriod: Wei
        recipientAddress: WalletAddress
        numPeriods: int
        periodLength: int
        startTime: NotRequired[int]

    _action: str

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount_s = format_value(fmt["amountPerPeriod"])
        import datetime

        fields: list[tuple[str, str, bool]] = [
            (
                "Payment Interval",
                humanize.naturaldelta(datetime.timedelta(seconds=args["periodLength"])),
                False,
            ),
        ]
        if "startTime" in args:
            fields.append(
                (
                    "First Payment",
                    f"<t:{args['startTime'] + args['periodLength']}>",
                    False,
                )
            )
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=f":satellite_orbital: pDAO Bootstrap Mode: {self._action} Recurring Spend",
                description=(
                    f"{fmt['recipientAddress']} will be awarded "
                    f"**{args['numPeriods']} x {amount_s} RPL**!"
                ),
                fields=fields,
            )
        ]


class BootstrapPDAOTreasuryNewEvent(_BootstrapPDAOTreasuryRecurringEvent):
    event_name = "bootstrap_pdao_spend_treasury_recurring_new_event"
    _action = "New"


class BootstrapPDAOTreasuryUpdateEvent(_BootstrapPDAOTreasuryRecurringEvent):
    event_name = "bootstrap_pdao_spend_treasury_recurring_update_event"
    _action = "Updated"


class BootstrapSDAOMemberInviteEvent(LogEvent):
    event_name = "bootstrap_sdao_member_invite_event"

    class Args(LogEventContext):
        memberAddress: WalletAddress
        id: str

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":satellite_orbital: pDAO Bootstrap Mode: Security Council Invite",
                description=(
                    f"**{args['id']}** ({fmt['memberAddress']}) has been invited "
                    f"to join the security council!"
                ),
            )
        ]


class BootstrapSDAOMemberKickEvent(LogEvent):
    event_name = "bootstrap_sdao_member_kick_event"

    class Args(LogEventContext):
        memberAddress: NodeAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        member_link = await el_explorer_url(
            args["memberAddress"], block=(args["blockNumber"] - 1)
        )
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":satellite_orbital: pDAO Bootstrap Mode: Kicked Security Council Member",
                description=(
                    f"{member_link} has been removed from the security council!"
                ),
            )
        ]


class BootstrapPDAODisableEvent(LogEvent):
    event_name = "bootstrap_pdao_disable_event"

    async def build_embeds(
        self, args: Any, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":satellite_orbital: pDAO Bootstrap Mode Disabled",
                description=(
                    "Bootstrap mode for the pDAO is now disabled! The guardian has "
                    "handed off full control over the Protocol DAO to on-chain governance!"
                ),
            )
        ]


class BootstrapPDAOEnableGovernanceEvent(LogEvent):
    event_name = "bootstrap_pdao_enable_governance_event"

    async def build_embeds(
        self, args: Any, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":satellite_orbital: pDAO Bootstrap Mode: Enable Governance",
                description="On-chain governance has been enabled!",
            )
        ]


# ===================================================================
# Group 6: DAO Proposal Events
# ===================================================================

DAO_NAME_TO_PREFIX: dict[str, str] = {
    "rocketDAONodeTrustedProposals": "odao",
    "rocketDAOSecurityProposals": "sdao",
}


_DAO_TITLES: dict[str, dict[str, str]] = {
    "add": {
        "odao": ":bulb: New oDAO Proposal",
        "sdao": ":bulb: New Security Council Proposal",
    },
    "vote": {
        "odao": ":ballot_box: oDAO Vote",
        "sdao": ":ballot_box: Security Council Vote",
    },
    "cancel": {
        "odao": ":no_entry_sign: oDAO Proposal Canceled",
        "sdao": ":no_entry_sign: Security Council Proposal Canceled",
    },
}


class _DAOProposalEvent(LogEvent):
    """Parameterized oDAO/sDAO proposal event (add/vote/cancel)."""

    class Args(LogEventContext):
        proposalID: int
        proposer: NotRequired[NodeAddress]
        voter: NotRequired[NodeAddress]
        supported: NotRequired[bool]
        canceller: NotRequired[NodeAddress]

    def __init__(self, event_name: str, action: str) -> None:
        self.event_name = event_name
        self._action = action

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        proposal_id = args["proposalID"]
        dao_name = await rp.call("rocketDAOProposal.getDAO", proposal_id)
        prefix = DAO_NAME_TO_PREFIX.get(dao_name)
        if prefix is None:
            return []
        dao = DefaultDAO(dao_name)
        proposal = await dao.fetch_proposal(proposal_id)
        body = await dao.build_proposal_body(
            proposal,
            include_proposer=False,
            include_payload=(self._action == "add"),
            include_votes=(self._action != "add"),
        )

        match self._action:
            case "add":
                desc = f"{fmt['proposer']} created **proposal #{proposal_id}**!"
            case "vote":
                decision = "for" if args["supported"] else "against"
                desc = f"{fmt['voter']} voted {decision} **proposal #{proposal_id}**!"
            case "cancel":
                desc = f"{fmt['canceller']} canceled **proposal #{proposal_id}**!"
            case _:
                desc = ""

        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=_DAO_TITLES[self._action][prefix],
                description=f"{desc}\n```{body}```",
            )
        ]


# ===================================================================
# Group 7: pDAO Proposal Events
# ===================================================================


async def _enrich_pdao_proposal(
    args: Mapping[str, Any],
    event_name: str,
    block_number: int,
) -> str | None:
    """Shared enrichment for pdao_proposal_* events.

    Returns the proposal body string, or ``None`` to filter the event.
    """
    proposal_id = _get_proposal_id(args)

    if "root" in event_name:
        challenge_state = await rp.call(
            "rocketDAOProtocolVerifier.getChallengeState",
            proposal_id,
            args["index"],
            block=block_number,
        )
        if challenge_state != 1:
            return None

    dao = ProtocolDAO()
    proposal = await dao.fetch_proposal(proposal_id)
    body = await dao.build_proposal_body(
        proposal,
        include_proposer=False,
        include_payload=("add" in event_name),
        include_votes=all(
            kw not in event_name for kw in ("add", "challenge", "root", "destroy")
        ),
    )
    return str(body) if body is not None else None


class PDAOProposalAddEvent(LogEvent):
    event_name = "pdao_proposal_add_event"

    class Args(LogEventContext):
        proposer: NodeAddress
        proposalID: NotRequired[int]
        proposalId: NotRequired[int]

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        body = await _enrich_pdao_proposal(args, self.event_name, args["blockNumber"])
        if body is None:
            return []
        proposal_id = _get_proposal_id(args)
        proposal_bond = solidity.to_int(
            await rp.call("rocketDAOProtocolVerifier.getProposalBond", proposal_id)
        )
        fmt = await self._fmt(args)
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":bulb: New pDAO Proposal",
                description=(
                    f"{fmt['proposer']} created **proposal #{proposal_id}**!\n```{body}```"
                ),
                fields=[("Proposal Bond", f"{proposal_bond} RPL", True)],
            )
        ]


class PDAOProposalVoteEvent(LogEvent):
    event_name = "pdao_proposal_vote_event"

    class Args(LogEventContext):
        voter: NodeAddress
        votingPower: Wei
        direction: int
        proposalID: NotRequired[int]
        proposalId: NotRequired[int]

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        voting_power = fmt["votingPower"]
        if voting_power < 250:
            return []

        body = await _enrich_pdao_proposal(args, self.event_name, args["blockNumber"])
        if body is None:
            return []
        proposal_id = _get_proposal_id(args)
        decision = ["invalid", "abstain", "for", "against", "against with veto"][
            args["direction"]
        ]
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":ballot_box: Major pDAO Vote",
                description=(
                    f"**Proposal #{proposal_id}**:\n"
                    f"{fmt['voter']} voted `{decision}` with a voting power of "
                    f"**{format_value(voting_power)}**!\n```{body}```"
                ),
            )
        ]


class PDAOProposalVoteOverrideEvent(LogEvent):
    event_name = "pdao_proposal_vote_override_event"

    class Args(LogEventContext):
        voter: NodeAddress
        delegate: NodeAddress
        direction: int
        proposalID: NotRequired[int]
        proposalId: NotRequired[int]

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        proposal_id = _get_proposal_id(args)
        proposal_block = await rp.call(
            "rocketDAOProtocolProposal.getProposalBlock", proposal_id
        )
        voting_power = solidity.to_float(
            await rp.call(
                "rocketNetworkVoting.getVotingPower", args["voter"], proposal_block
            )
        )
        if voting_power < 100:
            return []

        body = await _enrich_pdao_proposal(args, self.event_name, args["blockNumber"])
        if body is None:
            return []
        decision = ["invalid", "abstain", "for", "against", "against with veto"][
            args["direction"]
        ]
        fmt = await self._fmt(args)
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":person_gesturing_no: pDAO Delegate Override",
                description=(
                    f"**Proposal #{proposal_id}**:\n"
                    f"{fmt['voter']} overrode their delegate {fmt['delegate']} to vote `{decision}` "
                    f"with a voting power of **{format_value(voting_power)}**!\n```{body}```"
                ),
            )
        ]


class PDAOProposalFinaliseEvent(LogEvent):
    event_name = "pdao_proposal_finalise_event"

    class Args(LogEventContext):
        proposalID: NotRequired[int]
        proposalId: NotRequired[int]

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        body = await _enrich_pdao_proposal(args, self.event_name, args["blockNumber"])
        if body is None:
            return []
        proposal_id = _get_proposal_id(args)
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":x: pDAO Proposal Finalized",
                description=(
                    f"**Proposal #{proposal_id}** has been finalized "
                    f"after a veto vote!\n```{body}```"
                ),
            )
        ]


class PDAOProposalDestroyEvent(LogEvent):
    event_name = "pdao_proposal_destroy_event"

    class Args(LogEventContext):
        proposalID: NotRequired[int]
        proposalId: NotRequired[int]

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        body = await _enrich_pdao_proposal(args, self.event_name, args["blockNumber"])
        if body is None:
            return []
        proposal_id = _get_proposal_id(args)
        proposal_bond = solidity.to_int(
            await rp.call("rocketDAOProtocolVerifier.getProposalBond", proposal_id)
        )
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":bomb: pDAO Proposal Destroyed",
                description=(
                    f"**Proposal #{proposal_id}** has been destroyed "
                    f"after a successful challenge!\n```{body}```"
                ),
                fields=[("Proposal Bond", f"{proposal_bond} RPL", True)],
            )
        ]


class PDAOProposalRootEvent(LogEvent):
    event_name = "pdao_proposal_root_event"

    class Args(LogEventContext):
        proposer: NodeAddress
        index: int
        proposalID: NotRequired[int]
        proposalId: NotRequired[int]

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        body = await _enrich_pdao_proposal(args, self.event_name, args["blockNumber"])
        if body is None:
            return []
        proposal_id = _get_proposal_id(args)
        proposal_bond = solidity.to_int(
            await rp.call("rocketDAOProtocolVerifier.getProposalBond", proposal_id)
        )
        challenge_bond = solidity.to_int(
            await rp.call("rocketDAOProtocolVerifier.getChallengeBond", proposal_id)
        )
        challenge_period = await rp.call(
            "rocketDAOProtocolVerifier.getChallengePeriod", proposal_id
        )
        fmt = await self._fmt(args)
        import datetime

        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":shield: pDAO Proposal Defense",
                description=(
                    f"**Proposal #{proposal_id}**:\n"
                    f"{fmt['proposer']} responded to a challenge by submitting a new root!"
                    f"\n```{body}```"
                ),
                fields=[
                    ("Index", str(args["index"]), True),
                    ("Proposal Bond", f"{proposal_bond} RPL", True),
                    ("Challenge Bond", f"{challenge_bond} RPL", True),
                    (
                        "Challenge Period",
                        humanize.naturaldelta(
                            datetime.timedelta(seconds=challenge_period)
                        ),
                        True,
                    ),
                ],
            )
        ]


class PDAOProposalChallengeEvent(LogEvent):
    event_name = "pdao_proposal_challenge_event"

    class Args(LogEventContext):
        challenger: NodeAddress
        index: NotRequired[int]
        proposalID: NotRequired[int]
        proposalId: NotRequired[int]

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        body = await _enrich_pdao_proposal(args, self.event_name, args["blockNumber"])
        if body is None:
            return []
        proposal_id = _get_proposal_id(args)
        proposal_bond = solidity.to_int(
            await rp.call("rocketDAOProtocolVerifier.getProposalBond", proposal_id)
        )
        challenge_bond = solidity.to_int(
            await rp.call("rocketDAOProtocolVerifier.getChallengeBond", proposal_id)
        )
        challenge_period = await rp.call(
            "rocketDAOProtocolVerifier.getChallengePeriod", proposal_id
        )
        fmt = await self._fmt(args)
        import datetime

        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":crossed_swords: pDAO Proposal Challenge",
                description=(
                    f"{fmt['challenger']} challenged **proposal #{proposal_id}**!\n```{body}```"
                ),
                fields=[
                    ("Index", str(args.get("index", "")), True),
                    ("Proposal Bond", f"{proposal_bond} RPL", True),
                    ("Challenge Bond", f"{challenge_bond} RPL", True),
                    (
                        "Challenge Period",
                        humanize.naturaldelta(
                            datetime.timedelta(seconds=challenge_period)
                        ),
                        True,
                    ),
                ],
            )
        ]


class PDAOProposalBondBurnEvent(LogEvent):
    event_name = "pdao_proposal_bond_burn_event"

    class Args(LogEventContext):
        amount: Wei
        proposer: NodeAddress
        proposalID: NotRequired[int]
        proposalId: NotRequired[int]

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        body = await _enrich_pdao_proposal(args, self.event_name, args["blockNumber"])
        if body is None:
            return []
        proposal_id = _get_proposal_id(args)
        fmt = await self._fmt(args)
        amount_s = format_value(fmt["amount"])
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":fire: pDAO Proposal Bond Burned",
                description=(
                    f"**Proposal #{proposal_id}**:\n"
                    f"The **{amount_s} RPL** bond posted by {fmt['proposer']} has been burned!"
                ),
            )
        ]


# ===================================================================
# Group 8: DAO Member Events
# ===================================================================


class ODAOMemberJoinEvent(LogEvent):
    event_name = "odao_member_join_event"

    class Args(LogEventContext):
        nodeAddress: NodeAddress
        rplBondAmount: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        bond = format_value(fmt["rplBondAmount"])
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":new: oDAO Member Joined",
                description=(
                    f"{fmt['nodeAddress']} joined the oDAO with a bond of **{bond} RPL**!"
                ),
            )
        ]


class ODAOMemberLeaveEvent(LogEvent):
    event_name = "odao_member_leave_event"

    class Args(LogEventContext):
        nodeAddress: NodeAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        node_link = await el_explorer_url(
            args["nodeAddress"], block=(args["blockNumber"] - 1)
        )
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":door: oDAO Member Left",
                description=f"{node_link} left the oDAO!",
            )
        ]


class ODAOMemberKickEvent(LogEvent):
    event_name = "odao_member_kick_event"

    class Args(LogEventContext):
        nodeAddress: NodeAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        node_link = await el_explorer_url(
            args["nodeAddress"], block=(args["blockNumber"] - 1)
        )
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":boot: oDAO Member Kicked",
                description=f"{node_link} was kicked from the oDAO!",
            )
        ]


class ODAOMemberChallengeEvent(LogEvent):
    event_name = "odao_member_challenge_event"

    class Args(LogEventContext):
        nodeChallengedAddress: NodeAddress
        nodeChallengerAddress: NodeAddress
        time: int

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        deadline = args["time"] + await rp.call(
            "rocketDAONodeTrustedSettingsMembers.getChallengeWindow"
        )
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":rotating_light: oDAO Member Challenge Started",
                description=(
                    f"{fmt['nodeChallengedAddress']} has been challenged by {fmt['nodeChallengerAddress']}!\n"
                    f"They have to respond before <t:{deadline}:f> (<t:{deadline}:R>) "
                    f"or they will be kicked from the oDAO!"
                ),
            )
        ]


class ODAOMemberChallengeDecisionEvent(LogEvent):
    event_name = "odao_member_challenge_decision_event"

    class Args(LogEventContext):
        nodeChallengedAddress: NodeAddress
        nodeChallengeDeciderAddress: WalletAddress
        success: bool

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        challenged = fmt["nodeChallengedAddress"]
        if args["success"]:
            args["event_name"] = "odao_member_challenge_accepted_event"
            rpl_bond = format_value(
                solidity.to_float(
                    await rp.call(
                        "rocketDAONodeTrusted.getMemberRPLBondAmount",
                        args["nodeChallengedAddress"],
                        block=args["blockNumber"] - 1,
                    )
                )
            )
            return [
                await build_rich_event_embed(
                    tx_hash=args["transactionHash"],
                    block_number=args["blockNumber"],
                    receipt=receipt,
                    sender=args["nodeChallengeDeciderAddress"],
                    caller=None,
                    title=":warning: oDAO Member Challenge Passed",
                    description=(
                        f"{challenged} has been successfully challenged!\n"
                        f"Their bond of {rpl_bond} RPL has been burned "
                        f"and they have been kicked out of the oDAO!"
                    ),
                )
            ]
        else:
            args["event_name"] = "odao_member_challenge_rejected_event"
            return [
                await build_rich_event_embed(
                    tx_hash=args["transactionHash"],
                    block_number=args["blockNumber"],
                    receipt=receipt,
                    sender=None,
                    caller=None,
                    title=":no_entry_sign: oDAO Member Challenge Rejected",
                    description=(
                        f"{challenged} has responded to the challenge, making it invalid!"
                    ),
                )
            ]


class SDAOMemberJoinEvent(LogEvent):
    event_name = "sdao_member_join_event"

    class Args(LogEventContext):
        nodeAddress: WalletAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":new: Security Council Induction",
                description=f"{fmt['nodeAddress']} has joined the security council!",
            )
        ]


class SDAOMemberLeaveEvent(LogEvent):
    event_name = "sdao_member_leave_event"

    class Args(LogEventContext):
        nodeAddress: WalletAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        node_link = await el_explorer_url(
            args["nodeAddress"], block=(args["blockNumber"] - 1)
        )
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":door: Security Council Resignation",
                description=f"{node_link} has left the security council!",
            )
        ]


class SDAOMemberRequestLeaveEvent(LogEvent):
    event_name = "sdao_member_request_leave_event"

    class Args(LogEventContext):
        nodeAddress: WalletAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        node_link = await el_explorer_url(
            args["nodeAddress"], block=(args["blockNumber"] - 1)
        )
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":door: Security Council Resignation Request",
                description=(
                    f"{node_link} has requested to leave the security council!"
                ),
            )
        ]


# ===================================================================
# Group 9: Reward Events
# ===================================================================


class ODAORewardsSnapshotEvent(LogEvent):
    event_name = "odao_rewards_snapshot_event"

    class Args(LogEventContext):
        rewardIndex: int
        intervalStartTime: int
        intervalEndTime: int
        submission: Any

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        submission = dict(zip(SUBMISSION_KEYS, args["submission"], strict=False))
        fields: list[tuple[str, str, bool]] = []
        if "merkleTreeCID" in submission:
            n = f"0x{s_hex(submission['merkleRoot'].hex())}"
            fields.append(
                (
                    "Merkle Tree",
                    f"[{n}](https://gateway.ipfs.io/ipfs/{submission['merkleTreeCID']})",
                    True,
                )
            )
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":camera_with_flash: Reward Snapshot Published",
                description=(
                    f"Snapshot #{args['rewardIndex']} has been published by the oDAO!\n"
                    f"It spans from <t:{args['intervalStartTime']}> "
                    f"till <t:{args['intervalEndTime']}>"
                ),
                fields=fields or None,
            )
        ]


class ODAORewardsSnapshotSubmissionEvent(LogEvent):
    event_name = "odao_rewards_snapshot_submission_event"

    class Args(FromNodeContext):
        rewardIndex: int
        submission: Any

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        submission = dict(zip(SUBMISSION_KEYS, args["submission"], strict=False))
        from_link = fmt["from"]
        fields: list[tuple[str, str, bool]] = []
        if "merkleTreeCID" in submission:
            n = f"0x{s_hex(submission['merkleRoot'].hex())}"
            fields.append(
                (
                    "Merkle Tree",
                    f"[{n}](https://gateway.ipfs.io/ipfs/{submission['merkleTreeCID']})",
                    True,
                )
            )
        return [
            await build_rich_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                receipt=receipt,
                sender=args["from"],
                caller=receipt["from"],
                title=":writing_hand: Reward Snapshot Submission Submitted",
                description=(
                    f"{from_link} has published their submission for "
                    f"snapshot #{args['rewardIndex']}"
                ),
                fields=fields or None,
            )
        ]


# ===================================================================
# Group 10: Node Events
# ===================================================================


class NodeRegisterEvent(LogEvent):
    event_name = "node_register_event"

    class Args(LogEventContext):
        node: NodeAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        timezone = await rp.call(
            "rocketNodeManager.getNodeTimezoneLocation", args["node"]
        )
        fmt = await self._fmt(args)
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":mailbox_with_mail: Node Registered",
                description=f"{fmt['node']} registered as a node operator!",
                fields=[("Timezone", f"`{timezone}`", False)],
            )
        ]


class NodeSmoothingPoolStateChangedEvent(LogEvent):
    event_name = "node_smoothing_pool_state_changed"

    class Args(LogEventContext):
        node: NodeAddress
        state: bool

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        validator_count = await rp.call(
            "rocketMinipoolManager.getNodeMinipoolCount", args["node"]
        )
        megapool_address = await rp.call(
            "rocketNodeManager.getMegapoolAddress", args["node"]
        )
        if megapool_address != ADDRESS_ZERO:
            validator_count += await rp.call(
                "rocketMegapoolDelegate.getActiveValidatorCount",
                address=megapool_address,
            )

        fmt = await self._fmt(args)
        if args["state"]:
            args["event_name"] = "node_smoothing_pool_joined"
            return [
                await build_small_event_embed(
                    f":cup_with_straw: {fmt['node']} joined the smoothing pool "
                    f"with their {validator_count} validators!",
                    args["transactionHash"],
                )
            ]
        else:
            args["event_name"] = "node_smoothing_pool_left"
            return [
                await build_small_event_embed(
                    f":cup_with_straw: {fmt['node']} has left the smoothing pool "
                    f"with their {validator_count} validators!",
                    args["transactionHash"],
                )
            ]


# ===================================================================
# Group 11: Minipool Events (Global)
# ===================================================================


class MinipoolScrubEvent(LogEvent):
    event_name = "minipool_scrub_event"
    is_global = True

    class Args(MinipoolFromCallerContext):
        pass

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        minipool = args["minipool"]
        minipool_link = await _addr(minipool)
        is_vacant = await rp.call("rocketMinipoolDelegate.getVacant", address=minipool)

        if is_vacant:
            args["event_name"] = "vacant_minipool_scrub_event"
            pubkey_hex = (
                await rp.call("rocketMinipoolManager.getMinipoolPubkey", minipool)
            ).hex()
            vali_info = (await bacon.get_validator(f"0x{pubkey_hex}"))["data"]
            reason = "joe fucking up (unknown reason)"
            if vali_info:
                if all(
                    [
                        vali_info["validator"]["withdrawal_credentials"][:4] == "0x01",
                        vali_info["validator"]["withdrawal_credentials"][-40:]
                        != minipool[2:],
                    ]
                ):
                    reason = (
                        "having invalid withdrawal credentials set on the beacon chain"
                    )
                configured_balance = solidity.to_float(
                    await rp.call(
                        "rocketMinipoolDelegate.getPreMigrationBalance",
                        address=minipool,
                        block=args["blockNumber"] - 1,
                    )
                )
                if (
                    solidity.to_float(vali_info["balance"], 9) - configured_balance
                ) < -0.01:
                    reason = "having a balance lower than configured in the minipool contract on the beacon chain"
                if vali_info["status"] != "active_ongoing":
                    reason = "not being active on the beacon chain"
                scrub_period = await rp.call(
                    "rocketDAONodeTrustedSettingsMinipool.getPromotionScrubPeriod",
                    block=args["blockNumber"] - 1,
                )
                minipool_creation = await rp.call(
                    "rocketMinipoolDelegate.getStatusTime",
                    address=minipool,
                    block=args["blockNumber"] - 1,
                )
                block_time = await block_to_ts(args["blockNumber"] - 1)
                if block_time - minipool_creation > scrub_period // 2:
                    reason = "taking too long to migrate their withdrawal credentials on the beacon chain"

            embed = await build_rich_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                receipt=receipt,
                sender=args.get("from"),
                caller=args.get("caller"),
                title=":rotating_light: Vacant Minipool Scrubbed",
                description=(
                    f"Vacant Minipool {minipool_link} has been scrubbed "
                    f"likely due to **{reason}**!"
                ),
            )
        else:
            embed = await build_rich_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                receipt=receipt,
                sender=args.get("from"),
                caller=args.get("caller"),
                title=":rotating_light: Minipool Scrubbed",
                description=(
                    f"Minipool {minipool_link} has been scrubbed, likely due to "
                    f"having invalid withdrawal credentials on the beacon chain!"
                ),
            )

        embed.set_image(url="https://c.tenor.com/p3hWK5YRo6IAAAAC/this-is-fine-dog.gif")
        return [embed]


class MinipoolScrubVoteEvent(LogEvent):
    event_name = "minipool_scrub_vote_event"
    is_global = True

    class Args(MinipoolEventContext):
        member: NodeAddress

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        minipool = args["minipool"]
        minipool_link = await _addr(minipool)
        is_vacant = await rp.call("rocketMinipoolDelegate.getVacant", address=minipool)
        if is_vacant:
            args["event_name"] = "vacant_minipool_scrub_vote_event"
            return [
                await build_small_event_embed(
                    f":warning: {fmt['member']} has voted to scrub "
                    f"vacant minipool {minipool_link}!",
                    args["transactionHash"],
                )
            ]
        return [
            await build_small_event_embed(
                f":warning: {fmt['member']} has voted to scrub "
                f"minipool {minipool_link}!",
                args["transactionHash"],
            )
        ]


class MinipoolDepositReceivedEvent(LogEvent):
    event_name = "minipool_deposit_received_event"
    is_global = True

    class Args(MinipoolFromCallerContext):
        pass

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        minipool = args["minipool"]
        deposit_amount = await rp.call(
            "rocketMinipool.getNodeDepositBalance",
            address=minipool,
            block=args["blockNumber"],
        )

        node = receipt["from"]

        # Determine user deposit vs credit/balance usage
        user_deposit = deposit_amount
        ee = (
            await rp.get_contract_by_name("rocketNodeDeposit")
        ).events.DepositReceived()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            processed_logs = ee.process_receipt(receipt)
        for deposit_event in processed_logs:
            if (
                event.get("logIndex", 0) - 6
                < deposit_event.logIndex
                < event.get("logIndex", 0)
            ):
                user_deposit = deposit_event.args["amount"]

        minipool_link = await _addr(minipool)
        deposit_s = format_value(solidity.to_float(deposit_amount))
        event_name = self.event_name

        if user_deposit < deposit_amount:
            credit_amount = deposit_amount - user_deposit
            balance_amount = 0
            e = (await rp.get_contract_by_name("rocketVault")).events.EtherWithdrawn()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                vault_logs = e.process_receipt(receipt)

            deposit_contract = bytes(
                w3.solidity_keccak(["string"], ["rocketNodeDeposit"])
            )
            for withdraw_event in vault_logs:
                if (
                    event.get("logIndex", 0) - 7
                    < withdraw_event.logIndex
                    < event.get("logIndex", 0)
                    and withdraw_event.args["by"] == deposit_contract
                ):
                    balance_amount = withdraw_event.args["amount"]
                    credit_amount -= balance_amount
                    break

            if balance_amount == 0:
                event_name += "_credit"
                credit_s = format_value(solidity.to_float(credit_amount))
                embed = await build_rich_event_embed(
                    tx_hash=args["transactionHash"],
                    block_number=args["blockNumber"],
                    receipt=receipt,
                    sender=node,
                    caller=args.get("caller"),
                    title=":magic_wand: Minipool Created Using Credit",
                    description=(
                        f"Minipool {minipool_link} has been created with an operator "
                        f"share of **{deposit_s} ETH**!\n"
                        f"**{credit_s} ETH** worth of credit used for this deposit!"
                    ),
                )
            elif credit_amount == 0:
                event_name += "_balance"
                balance_s = format_value(solidity.to_float(balance_amount))
                embed = await build_rich_event_embed(
                    tx_hash=args["transactionHash"],
                    block_number=args["blockNumber"],
                    receipt=receipt,
                    sender=node,
                    caller=args.get("caller"),
                    title=":magic_wand: Minipool Created Using ETH Balance",
                    description=(
                        f"Minipool {minipool_link} has been created with an operator "
                        f"share of **{deposit_s} ETH**!\n"
                        f"**{balance_s} ETH** of the node's existing balance was "
                        f"used for this deposit!"
                    ),
                )
            else:
                event_name += "_shared"
                credit_s = format_value(solidity.to_float(credit_amount))
                balance_s = format_value(solidity.to_float(balance_amount))
                embed = await build_rich_event_embed(
                    tx_hash=args["transactionHash"],
                    block_number=args["blockNumber"],
                    receipt=receipt,
                    sender=node,
                    caller=args.get("caller"),
                    title=":magic_wand: Minipool Created Using Credit",
                    description=(
                        f"Minipool {minipool_link} has been created with an operator "
                        f"share of **{deposit_s} ETH**!\n"
                        f"**{credit_s} ETH** of credit and **{balance_s} ETH** "
                        f"of the node's balance were used for this deposit!"
                    ),
                )
        else:
            embed = await build_rich_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                receipt=receipt,
                sender=node,
                caller=args.get("caller"),
                title=":construction_site: Minipool Created",
                description=(
                    f"Minipool {minipool_link} has been created with an operator "
                    f"share of **{deposit_s} ETH**!"
                ),
            )

        args["event_name"] = event_name
        return [embed]


class MinipoolVacancyPreparedEvent(LogEvent):
    event_name = "minipool_vacancy_prepared_event"
    is_global = True

    class Args(MinipoolFromCallerContext):
        bondAmount: Wei

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        pubkey_link = await cl_explorer_url(args.get("pubkey", ""))
        minipool_link = await _addr(args["minipool"])
        bond_s = format_value(fmt["bondAmount"])
        return [
            await build_rich_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                receipt=receipt,
                sender=args.get("from"),
                caller=args.get("caller"),
                title=":link: Solo Migration Initiated",
                description=(
                    f"Migration of solo validator {pubkey_link} to minipool "
                    f"{minipool_link} with a bond of **{bond_s} ETH** was initiated!"
                ),
            )
        ]


class MinipoolWithdrawalProcessedEvent(LogEvent):
    event_name = "minipool_withdrawal_processed_event"
    is_global = True

    class Args(MinipoolEventContext):
        nodeAmount: Wei
        userAmount: Wei

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        contract = await rp.assemble_contract("rocketMinipool", args["minipool"])
        node = await contract.functions.getNodeAddress().call()
        total = fmt["nodeAmount"] + fmt["userAmount"]
        minipool_link = await _addr(args["minipool"])
        node_link = await _addr(node)
        return [
            await build_small_event_embed(
                f":moneybag: **{format_value(total)} ETH** has been distributed "
                f"from minipool {minipool_link}, owned by operator {node_link}!",
                args["transactionHash"],
            )
        ]


class MinipoolStatusUpdatedEvent(LogEvent):
    """Only status=4 (dissolved) is reported; everything else is filtered."""

    event_name = "minipool_status_updated_event"
    is_global = True

    class Args(MinipoolEventContext):
        status: int

    async def resolve(
        self, args: dict[str, Any], event: LogEventData
    ) -> LogEvent | None:
        if args.get("status") == 4:
            return _minipool_dissolve_event
        return None

    async def build_embeds(
        self, args: Any, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        raise RuntimeError("Must be resolved first")


class MinipoolDissolveEvent(LogEvent):
    event_name = "minipool_dissolve_event"
    is_global = True

    class Args(MinipoolFromCallerContext):
        pass

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        operator = await rp.call(
            "rocketMinipoolDelegate.getNodeAddress", address=args["minipool"]
        )
        minipool_link = await _addr(args["minipool"])
        operator_link = await _addr(operator)
        embed = await build_rich_event_embed(
            tx_hash=args["transactionHash"],
            block_number=args["blockNumber"],
            receipt=receipt,
            sender=args.get("from"),
            caller=args.get("caller"),
            title=":rotating_light: Minipool Dissolved",
            description=(
                f"Minipool {minipool_link} owned by operator {operator_link} "
                f"failed to stake its assigned ETH and has been dissolved!"
            ),
        )
        embed.set_image(url="https://c.tenor.com/p3hWK5YRo6IAAAAC/this-is-fine-dog.gif")
        return [embed]


_minipool_dissolve_event = MinipoolDissolveEvent()


class MinipoolPenaltyUpdatedEvent(LogEvent):
    event_name = "minipool_penalty_updated"

    class Args(LogEventContext):
        minipoolAddress: MinipoolAddress
        penalty: Percentage

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        embed = await build_event_embed(
            tx_hash=args["transactionHash"],
            block_number=args["blockNumber"],
            title=":rotating_light: Minipool Penalty Updated",
            description=(
                f"Minipool {fmt['minipoolAddress']} has had its Penalty "
                f"increased to {format_value(fmt['penalty'])}%!"
            ),
        )
        embed.set_image(url="https://i.giphy.com/jmSjPi6soIoQCFwaXJ.webp")
        return [embed]


class ODAOMinipoolPenaltyEvent(LogEvent):
    event_name = "odao_minipool_penalty_updated"

    class Args(LogEventContext):
        rate: Percentage

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        embed = await build_event_embed(
            tx_hash=args["transactionHash"],
            block_number=args["blockNumber"],
            title=":rotating_light: Minipool Penalty",
            description=(
                f"The maximum minipool penalty rate has been raised "
                f"to {format_value(fmt['rate'])}%!"
            ),
        )
        embed.set_image(url="https://i.giphy.com/jmSjPi6soIoQCFwaXJ.webp")
        return [embed]


# ===================================================================
# Group 12: Megapool Events (Global)
# ===================================================================


class MegapoolValidatorAssignedEvent(LogEvent):
    event_name = "megapool_validator_assigned_event"
    is_global = True

    class Args(MegapoolEventContext):
        validatorId: int

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        count = event.get("assignmentCount", 1)
        fmt = await self._fmt(args)

        if count == 1:
            return [
                await build_small_event_embed(
                    f":handshake: Validator {args['validatorId']} of node "
                    f"{fmt['node']} has been assigned funds from the deposit pool!",
                    args["transactionHash"],
                )
            ]

        return [
            await build_small_event_embed(
                f":handshake: **{count} validators** of node "
                f"{fmt['node']} have been assigned funds from the deposit pool!",
                args["transactionHash"],
            )
        ]


class MegapoolValidatorExitingEvent(LogEvent):
    event_name = "megapool_validator_exiting_event"
    is_global = True

    class Args(MegapoolEventContext):
        validatorId: int

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        return [
            await build_small_event_embed(
                f":octagonal_sign: Validator {args['validatorId']} of node "
                f"{fmt['node']} has started exiting!",
                args["transactionHash"],
            )
        ]


class MegapoolValidatorExitedEvent(LogEvent):
    event_name = "megapool_validator_exited_event"
    is_global = True

    class Args(MegapoolEventContext):
        validatorId: int

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        return [
            await build_small_event_embed(
                f":leaves: Validator {args['validatorId']} of node "
                f"{fmt['node']} has exited!",
                args["transactionHash"],
            )
        ]


class MegapoolValidatorDissolveEvent(LogEvent):
    event_name = "megapool_validator_dissolve_event"
    is_global = True

    class Args(MegapoolFromCallerContext):
        validatorId: int

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        node_link = fmt["node"]
        embed = await build_rich_event_embed(
            tx_hash=args["transactionHash"],
            block_number=args["blockNumber"],
            receipt=receipt,
            sender=args.get("from"),
            caller=args.get("caller"),
            title=":rotating_light: Validator Dissolved",
            description=(
                f":leaves: Validator {args['validatorId']} of node "
                f"{node_link} has been dissolved!"
            ),
        )
        embed.set_image(url="https://c.tenor.com/p3hWK5YRo6IAAAAC/this-is-fine-dog.gif")
        return [embed]


class MegapoolPenaltyEvent(LogEvent):
    event_name = "megapool_penalty_event"
    is_global = True

    class Args(MegapoolFromCallerContext):
        amount: Wei

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount_s = format_value(fmt["amount"])
        embed = await build_rich_event_embed(
            tx_hash=args["transactionHash"],
            block_number=args["blockNumber"],
            receipt=receipt,
            sender=args.get("from"),
            caller=args.get("caller"),
            title=":police_car: Megapool Penalty Applied",
            description=(
                f"Node {fmt['node']} has been penalized for **{amount_s} ETH**!"
            ),
        )
        embed.set_image(url="https://i.giphy.com/jmSjPi6soIoQCFwaXJ.webp")
        return [embed]


# ===================================================================
# Group 13: Constellation Events
# ===================================================================

_NODESET_EMOJI = "<:nodeset:1351406340056285266>"


class _ConstellationVaultEvent(LogEvent):
    class Args(LogEventContext):
        sender: WalletAddress
        assets: Wei
        shares: Wei

    _action: str
    _verb: str
    _prep: str

    def __init__(
        self,
        event_name: str,
        unit_shares: str,
        unit_assets: str,
    ) -> None:
        self.event_name = event_name
        self._unit_shares = unit_shares
        self._unit_assets = unit_assets

    async def build_embeds(
        self,
        args: Args,
        event: LogEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        assets = fmt["assets"]
        shares = fmt["shares"]

        if self._unit_assets == "RPL":
            rpl_ratio = solidity.to_float(
                await rp.call("rocketNetworkPrices.getRPLPrice")
            )
            use_large = assets >= 16 / rpl_ratio
        else:
            use_large = assets >= 100

        assets_s = format_value(assets)
        shares_s = format_value(shares)

        if not use_large:
            return [
                await build_small_event_embed(
                    f"{_NODESET_EMOJI} {fmt['sender']} {self._verb} "
                    f"**{shares_s} {self._unit_shares}** "
                    f"{self._prep} **{assets_s} {self._unit_assets}**!",
                    args["transactionHash"],
                )
            ]
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=f"{_NODESET_EMOJI} {self._unit_shares} {self._action.capitalize()}",
                description=(
                    f"**{shares_s} {self._unit_shares}** {self._verb} "
                    f"{self._prep} **{assets_s} {self._unit_assets}**!"
                ),
            )
        ]


class ConstellationDepositEvent(_ConstellationVaultEvent):
    _action = "deposit"
    _verb = "minted"
    _prep = "from"


class ConstellationWithdrawEvent(_ConstellationVaultEvent):
    _action = "withdrawal"
    _verb = "burned"
    _prep = "for"


# ===================================================================
# Group 14: Upgrade Events (Global)
# ===================================================================


class ODAOUpgradePendingEvent(LogEvent):
    event_name = "odao_upgrade_pending_event"

    class Args(LogEventContext):
        upgradeProposalID: int

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        contract_name = await rp.call(
            "rocketDAONodeTrustedUpgrade.getName",
            args["upgradeProposalID"],
            block=args["blockNumber"],
        )
        contract_address: ChecksumAddress = w3.to_checksum_address(
            await rp.call(
                "rocketDAONodeTrustedUpgrade.getUpgradeAddress",
                args["upgradeProposalID"],
                block=args["blockNumber"],
            )
        )
        veto_deadline = await rp.call(
            "rocketDAONodeTrustedUpgrade.getEnd",
            args["upgradeProposalID"],
            block=args["blockNumber"],
        )

        if contract_address == ADDRESS_ZERO:
            args["event_name"] = "upgrade_pending_abi_event"
            return [
                await build_event_embed(
                    tx_hash=args["transactionHash"],
                    block_number=args["blockNumber"],
                    title=":hourglass: Contract Upgrade Pending",
                    description=(
                        f"The upgrade process for `{contract_name}` has been initiated.\n"
                        f"Veto window ends <t:{veto_deadline}:f> (<t:{veto_deadline}:R>)."
                    ),
                )
            ]
        else:
            addr_link = await _addr(contract_address)
            return [
                await build_event_embed(
                    tx_hash=args["transactionHash"],
                    block_number=args["blockNumber"],
                    title=":hourglass: Contract Upgrade Pending",
                    description=(
                        f"The upgrade process for `{contract_name}` has been initiated.\n"
                        f"Veto window ends <t:{veto_deadline}:f> (<t:{veto_deadline}:R>)."
                    ),
                    fields=[("Contract Address", addr_link, False)],
                )
            ]


class SDAOUpgradeVetoedEvent(LogEvent):
    event_name = "sdao_upgrade_vetoed_event"

    class Args(LogEventContext):
        upgradeProposalID: int

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        contract_name = await rp.call(
            "rocketDAONodeTrustedUpgrade.getName",
            args["upgradeProposalID"],
            block=args["blockNumber"],
        )
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":no_entry: Contract Upgrade Vetoed",
                description=(
                    f"Upgrade #{args['upgradeProposalID']} for `{contract_name}` "
                    f"has been vetoed by the security council!"
                ),
            )
        ]


class ODAOContractUpgradedEvent(LogEvent):
    event_name = "odao_contract_upgraded_event"

    class Args(LogEventContext):
        oldAddress: ContractAddress
        newAddress: ContractAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        contract_name = rp.get_name_by_address(args["oldAddress"]) or "Unknown"
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":page_facing_up: Contract Upgraded",
                description=f"`{contract_name}` has been upgraded to {fmt['newAddress']}.",
            )
        ]


class ODAOContractAddedEvent(LogEvent):
    event_name = "odao_contract_added_event"

    class Args(LogEventContext):
        newAddress: ContractAddress

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        contract_name = rp.get_name_by_address(args["newAddress"]) or "Unknown"
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":page_facing_up: Contract Added",
                description=f"New contract `{contract_name}` added at {fmt['newAddress']}.",
            )
        ]


# ===================================================================
# Group 15: Misc Events
# ===================================================================


class UnstETHWithdrawalEvent(LogEvent):
    event_name = "unsteth_withdrawal_requested_event"

    class Args(LogEventContext):
        owner: WalletAddress
        amountOfStETH: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount = fmt["amountOfStETH"]
        if amount < 10_000:
            return []
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":money_with_wings: Large stETH Withdrawal Requested",
                description=(
                    f"{fmt['owner']} has requested a withdrawal of "
                    f"**{format_value(amount)} stETH**!"
                ),
            )
        ]


class ExitArbitrageEvent(LogEvent):
    event_name = "exit_arbitrage_event"

    class Args(LogEventContext):
        caller: WalletAddress
        receiver: WalletAddress
        amount: Wei
        profit: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount = fmt["amount"]
        profit = fmt["profit"]
        receiver = fmt["receiver"]
        if amount < 100:
            return [
                await build_small_event_embed(
                    f":money_mouth: {receiver} earned "
                    f"**{format_value(profit)} ETH** from an exit arbitrage!",
                    args["transactionHash"],
                )
            ]
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":money_mouth: Large Exit Arbitrage",
                description=(
                    f"{receiver} earned **{format_value(profit)} ETH** "
                    f"with a {format_value(amount)} ETH flash loan!"
                ),
            )
        ]


class RockSolidDepositEvent(LogEvent):
    event_name = "rocksolid_deposit_event"

    class Args(LogEventContext):
        sender: WalletAddress
        assets: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        assets = fmt["assets"]
        assets_s = format_value(assets)
        _RS = "<:rocksolid:1425091714267480158>"

        if assets < 50:
            return [
                await build_small_event_embed(
                    f"{_RS} {fmt['sender']} deposited "
                    f"**{assets_s} rETH** into the RockSolid vault!",
                    args["transactionHash"],
                )
            ]
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=f"{_RS} RockSolid rETH Deposit",
                description=(
                    f"**{assets_s} rETH** deposited into the RockSolid vault!"
                ),
            )
        ]


class RockSolidWithdrawalEvent(LogEvent):
    event_name = "rocksolid_withdrawal_event"

    class Args(LogEventContext):
        sender: WalletAddress
        shares: Wei

    async def build_embeds(
        self, args: Args, event: LogEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        block = args["blockNumber"]
        assets_raw = await rp.call(
            "RockSolidVault.convertToAssets", args["shares"], block=block
        )
        assets = solidity.to_float(assets_raw)
        shares = fmt["shares"]
        assets_s = format_value(assets)
        _RS = "<:rocksolid:1425091714267480158>"

        if shares < 50:
            return [
                await build_small_event_embed(
                    f"{_RS} {fmt['sender']} requested a withdrawal for "
                    f"**{assets_s} rETH** from the RockSolid vault!",
                    args["transactionHash"],
                )
            ]
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=f"{_RS} RockSolid rETH Withdrawal",
                description=(
                    f"New withdrawal request for **{assets_s} rETH** "
                    f"from the RockSolid vault!"
                ),
            )
        ]


# ===================================================================
# Parameterized instances
# ===================================================================

# ===================================================================
# Registry — single source of truth for (contract, solidity_event) → handler
# ===================================================================

EVENT_REGISTRY: dict[str, dict[str, LogEvent]] = {
    "unstETH": {
        "WithdrawalRequested": UnstETHWithdrawalEvent(),
    },
    "rocketExitArbitrage": {
        "Arbitrage": ExitArbitrageEvent(),
    },
    "rocketNetworkBalances": {
        "BalancesUpdated": NegativeRETHRatioEvent(),
    },
    "rocketNetworkPrices": {
        "PricesUpdated": PriceUpdateEvent(),
    },
    "rocketMerkleDistributorMainnet": {
        "RewardsClaimed": NodeMerkleRewardsClaimedEvent(),
    },
    "rocketTokenRETH": {
        "Transfer": TransferEvent(),
        "TokensBurned": RETHBurnEvent(),
    },
    "rocketDepositPool": {
        "DepositReceived": PoolDepositEvent(),
        "DepositAssigned": PoolDepositAssignedEvent(),
        "DepositRecycled": PoolDepositRecycledEvent(),
        "QueueExited": ValidatorQueueExitedEvent(),
        "CreditWithdrawn": CreditWithdrawnEvent(),
    },
    "rocketNetworkPenalties": {
        "PenaltyUpdated": MinipoolPenaltyUpdatedEvent(),
    },
    "rocketDAOProposal": {
        "ProposalAdded": _DAOProposalEvent("dao_proposal_add_event", "add"),
        "ProposalVoted": _DAOProposalEvent("dao_proposal_vote_event", "vote"),
        "ProposalCancelled": _DAOProposalEvent("dao_proposal_cancel_event", "cancel"),
    },
    "rocketDAONodeTrustedActions": {
        "ActionJoined": ODAOMemberJoinEvent(),
        "ActionLeave": ODAOMemberLeaveEvent(),
        "ActionChallengeMade": ODAOMemberChallengeEvent(),
        "ActionChallengeDecided": ODAOMemberChallengeDecisionEvent(),
        "ActionKick": ODAOMemberKickEvent(),
    },
    "rocketNodeManager": {
        "NodeRegistered": NodeRegisterEvent(),
        "NodeSmoothingPoolStateChanged": NodeSmoothingPoolStateChangedEvent(),
    },
    "rocketTokenRPL": {
        "RPLInflationLog": RPLInflationEvent(),
        "RPLFixedSupplyBurn": RPLMigrationEvent(),
    },
    "rocketRewardsPool": {
        "RewardSnapshot": ODAORewardsSnapshotEvent(),
        "RewardSnapshotSubmitted": ODAORewardsSnapshotSubmissionEvent(),
    },
    "rocketAuctionManager": {
        "LotCreated": AuctionLotCreateEvent(),
        "BidPlaced": AuctionBidEvent(),
        "RPLRecovered": AuctionRPLRecoverEvent(),
    },
    "rocketNodeStaking": {
        "RPLStaked(address,address,uint256,uint256)": RPLStakeEvent(),
        "RPLWithdrawn": RPLWithdrawEvent(),
        "RPLSlashed": NodeRPLSlashEvent(),
    },
    "rocketMinipoolPenalty": {
        "MaxPenaltyRateUpdated": ODAOMinipoolPenaltyEvent(),
    },
    "rocketDAOProtocol": {
        "BootstrapSettingMulti": BootstrapPDAOSettingMultiEvent(),
        "BootstrapSettingUint": BootstrapPDAOSettingEvent(),
        "BootstrapSettingBool": BootstrapPDAOSettingEvent(),
        "BootstrapSettingAddress": BootstrapPDAOSettingEvent(),
        "BootstrapSettingClaimers": BootstrapPDAOClaimerEvent(),
        "BootstrapSpendTreasury": BootstrapPDAOSpendTreasuryEvent(),
        "BootstrapTreasuryNewContract": BootstrapPDAOTreasuryNewEvent(),
        "BootstrapTreasuryUpdateContract": BootstrapPDAOTreasuryUpdateEvent(),
        "BootstrapSecurityInvite": BootstrapSDAOMemberInviteEvent(),
        "BootstrapSecurityKick": BootstrapSDAOMemberKickEvent(),
        "BootstrapDisabled": BootstrapPDAODisableEvent(),
        "BootstrapProtocolDAOEnabled": BootstrapPDAOEnableGovernanceEvent(),
    },
    "rocketNodeDeposit": {
        "DepositFor": ETHDepositEvent(),
        "Withdrawal": ETHWithdrawEvent(),
        "DepositReceived": ValidatorDepositEvent(),
        "MultiDepositReceived": ValidatorMultiDepositEvent(),
    },
    "rocketDAOSecurityActions": {
        "ActionJoined": SDAOMemberJoinEvent(),
        "ActionLeave": SDAOMemberLeaveEvent(),
        "ActionRequestLeave": SDAOMemberRequestLeaveEvent(),
    },
    "rocketDAOProtocolProposal": {
        "ProposalAdded": PDAOProposalAddEvent(),
        "ProposalVoted": PDAOProposalVoteEvent(),
        "ProposalVoteOverridden": PDAOProposalVoteOverrideEvent(),
        "ProposalFinalised": PDAOProposalFinaliseEvent(),
        "ProposalDestroyed": PDAOProposalDestroyEvent(),
    },
    "rocketDAOProtocolVerifier": {
        "RootSubmitted": PDAOProposalRootEvent(),
        "ChallengeSubmitted": PDAOProposalChallengeEvent(),
        "ProposalBondBurned": PDAOProposalBondBurnEvent(),
    },
    "Constellation.ETHVault": {
        "Deposit": ConstellationDepositEvent(
            "cs_deposit_eth_event",
            "xrETH",
            "ETH",
        ),
        "Withdraw": ConstellationWithdrawEvent(
            "cs_withdraw_eth_event",
            "xrETH",
            "ETH",
        ),
    },
    "Constellation.RPLVault": {
        "Deposit": ConstellationDepositEvent(
            "cs_deposit_rpl_event",
            "xRPL",
            "RPL",
        ),
        "Withdraw": ConstellationWithdrawEvent(
            "cs_withdraw_rpl_event",
            "xRPL",
            "RPL",
        ),
    },
    "RockSolidVault": {
        "DepositSync": RockSolidDepositEvent(),
        "RedeemRequest": RockSolidWithdrawalEvent(),
    },
    "rocketDAONodeTrustedUpgrade": {
        "UpgradePending": ODAOUpgradePendingEvent(),
        "UpgradeVetoed": SDAOUpgradeVetoedEvent(),
        "ContractUpgraded": ODAOContractUpgradedEvent(),
        "ContractAdded": ODAOContractAddedEvent(),
    },
    # --- Global events ---
    "rocketMinipoolDelegate": {
        "MinipoolScrubbed": MinipoolScrubEvent(),
        "MinipoolPrestaked": MinipoolDepositReceivedEvent(),
        "ScrubVoted": MinipoolScrubVoteEvent(),
        "MinipoolVacancyPrepared": MinipoolVacancyPreparedEvent(),
        "EtherWithdrawalProcessed": MinipoolWithdrawalProcessedEvent(),
        "StatusUpdated": MinipoolStatusUpdatedEvent(),
    },
    "rocketMegapoolDelegate": {
        "MegapoolValidatorAssigned": MegapoolValidatorAssignedEvent(),
        "MegapoolValidatorExiting": MegapoolValidatorExitingEvent(),
        "MegapoolValidatorExited": MegapoolValidatorExitedEvent(),
        "MegapoolValidatorDissolved": MegapoolValidatorDissolveEvent(),
        "MegapoolPenaltyApplied": MegapoolPenaltyEvent(),
    },
}
