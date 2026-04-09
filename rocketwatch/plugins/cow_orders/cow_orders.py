import contextlib
import logging
from typing import TypedDict, cast

from discord import Color, Interaction
from discord.app_commands import command
from eth_typing import BlockNumber, ChecksumAddress, HexStr
from web3.contract import AsyncContract
from web3.types import EventData

from rocketwatch.bot import RocketWatch
from rocketwatch.utils import solidity
from rocketwatch.utils.embeds import (
    Embed,
    build_event_embed,
    el_explorer_url,
    format_value,
)
from rocketwatch.utils.event import Event, EventPlugin
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.sea_creatures import get_sea_creature_for_address
from rocketwatch.utils.shared_w3 import w3
from rocketwatch.utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.cow_orders")

_COW_EVENTS: dict[str, tuple[Color, str, str]] = {
    "cow_order_buy_rpl": (
        Color.from_rgb(86, 235, 86),
        ":cow: RPL Buy",
        "{owner} bought **{our} RPL** for {other} {token}!",
    ),
    "cow_order_buy_reth": (
        Color.from_rgb(86, 235, 86),
        ":cow: rETH Buy",
        "{owner} bought **{our} rETH** for {other} {token}!",
    ),
    "cow_order_sell_rpl": (
        Color.from_rgb(235, 86, 86),
        ":cow: RPL Sell",
        "{owner} sold **{our} RPL** for {other} {token}!",
    ),
    "cow_order_sell_reth": (
        Color.from_rgb(235, 86, 86),
        ":cow: rETH Sell",
        "{owner} sold **{our} rETH** for {other} {token}!",
    ),
}


class CoWTradeArgs(TypedDict):
    owner: ChecksumAddress
    sellToken: ChecksumAddress
    buyToken: ChecksumAddress
    sellAmount: int
    buyAmount: int
    feeAmount: int
    orderUid: bytes


class CoWOrders(EventPlugin):
    def __init__(self, bot: RocketWatch) -> None:
        super().__init__(bot)
        self._settlement: AsyncContract | None = None
        self._tokens: list[ChecksumAddress] | None = None

    async def _ensure_setup(self) -> None:
        if self._settlement is None:
            self._settlement = await rp.get_contract_by_name("GPv2Settlement")
        if self._tokens is None:
            self._tokens = [
                await rp.get_address_by_name("rocketTokenRPL"),
                await rp.get_address_by_name("rocketTokenRETH"),
            ]

    @command()
    async def cow(self, interaction: Interaction, etherscan_url: str) -> None:
        if "etherscan.io/tx/" not in etherscan_url:
            await interaction.response.send_message(
                "Invalid Etherscan URL", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=is_hidden(interaction))
        url = etherscan_url.replace("etherscan.io", "explorer.cow.fi")
        embed = Embed(description=f"[CoW Explorer]({url})")
        await interaction.followup.send(embed=embed)

    async def _get_new_events(self) -> list[Event]:
        from_block = self.last_served_block + 1 - self.lookback_distance
        return await self.get_past_events(BlockNumber(from_block), self._pending_block)

    async def get_past_events(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[Event]:
        await self._ensure_setup()
        assert self._settlement is not None
        assert self._tokens is not None

        trade_event = self._settlement.events.Trade()
        logs = await w3.eth.get_logs(
            {
                "address": self._settlement.address,
                "topics": [trade_event.topic],
                "fromBlock": from_block,
                "toBlock": to_block,
            }
        )

        if not logs:
            return []

        # decode logs into Trade events
        trades: list[EventData] = [trade_event.process_log(raw_log) for raw_log in logs]
        # filter for RPL and rETH trades
        trades = [
            trade
            for trade in trades
            if trade["args"]["sellToken"] in self._tokens
            or trade["args"]["buyToken"] in self._tokens
        ]

        if not trades:
            return []

        # get prices for USD threshold
        rpl_ratio = solidity.to_float(await rp.call("rocketNetworkPrices.getRPLPrice"))
        reth_ratio = solidity.to_float(await rp.call("rocketTokenRETH.getExchangeRate"))
        eth_usdc_price = await rp.get_eth_usdc_price()
        rpl_price: float = rpl_ratio * eth_usdc_price
        reth_price: float = reth_ratio * eth_usdc_price

        events: list[Event] = []
        for trade in trades:
            args = cast(CoWTradeArgs, trade["args"])

            sell_token: ChecksumAddress = args["sellToken"]
            buy_token: ChecksumAddress = args["buyToken"]

            if buy_token in self._tokens:
                token = "rETH" if buy_token == self._tokens[1] else "RPL"
                token_amount, other_amount = args["buyAmount"], args["sellAmount"]
                other_address = w3.to_checksum_address(args["sellToken"])
                event_name = f"cow_order_buy_{token.lower()}"
            else:
                token = "rETH" if sell_token == self._tokens[1] else "RPL"
                token_amount, other_amount = args["sellAmount"], args["buyAmount"]
                other_address = w3.to_checksum_address(args["buyToken"])
                event_name = f"cow_order_sell_{token.lower()}"

            our_amount = solidity.to_float(token_amount, 18)
            # skip trades under minimum value
            if ((token == "RPL") and (our_amount * rpl_price < 10_000)) or (
                (token == "rETH") and (our_amount * reth_price < 100_000)
            ):
                continue

            decimals = 18
            erc20 = await rp.assemble_contract(name="ERC20", address=other_address)
            with contextlib.suppress(Exception):
                decimals = await erc20.functions.decimals().call()

            other_amount_f = solidity.to_float(other_amount, decimals)

            try:
                other_token = await erc20.functions.symbol().call()
            except Exception:
                other_token = "UNKWN"
                if other_address == w3.to_checksum_address(
                    "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
                ):
                    other_token = "ETH"

            owner = w3.to_checksum_address(args["owner"])
            sea = await get_sea_creature_for_address(owner)
            owner_link = await el_explorer_url(owner, prefix=sea)
            cow_uid = f"0x{args['orderUid'].hex()}"
            cow_link = f"[ORDER](https://explorer.cow.fi/orders/{cow_uid})"
            tx_hash = HexStr(trade["transactionHash"].to_0x_hex())
            block_number = BlockNumber(trade["blockNumber"])

            color, title, desc_template = _COW_EVENTS[event_name]
            description = desc_template.format(
                owner=owner_link,
                our=format_value(our_amount),
                other=format_value(other_amount_f),
                token=other_token,
            )

            embed = await build_event_embed(
                tx_hash=tx_hash,
                block_number=block_number,
                color=color,
                title=title,
                description=description,
                fields=[("CoW Order", cow_link, False)],
            )

            events.append(
                Event(
                    embed=embed,
                    topic="cow_trade",
                    block_number=block_number,
                    event_name=event_name,
                    unique_id=f"cow_trade_{trade['transactionHash'].hex()}:{trade['logIndex']}",
                    transaction_index=trade["transactionIndex"],
                    event_index=trade["logIndex"],
                )
            )

        return events


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(CoWOrders(bot))
