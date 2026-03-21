import contextlib
import logging
from typing import Any, TypedDict, cast

from discord import Interaction
from discord.app_commands import command
from eth_typing import BlockNumber, ChecksumAddress
from hexbytes import HexBytes
from web3.contract import AsyncContract
from web3.datastructures import MutableAttributeDict as aDict
from web3.types import EventData

from rocketwatch import RocketWatch
from utils import solidity
from utils.embeds import Embed, assemble, prepare_args
from utils.event import Event, EventPlugin
from utils.rocketpool import rp
from utils.shared_w3 import w3
from utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.cow_orders")


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
        self._trade_topic: HexBytes | None = None
        self._tokens: list[str] | None = None

    async def _ensure_setup(self) -> None:
        if self._settlement is None:
            self._settlement = await rp.get_contract_by_name("GPv2Settlement")
            # Trade(address,address,address,uint256,uint256,uint256,bytes)
            self._trade_topic = w3.keccak(
                text="Trade(address,address,address,uint256,uint256,uint256,bytes)"
            )
        if self._tokens is None:
            self._tokens = [
                str(await rp.get_address_by_name("rocketTokenRPL")).lower(),
                str(await rp.get_address_by_name("rocketTokenRETH")).lower(),
            ]

    @command()
    async def cow(self, interaction: Interaction, tnx: str) -> None:
        if "etherscan.io/tx/" not in tnx:
            await interaction.response.send_message("nop", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=is_hidden(interaction))
        url = tnx.replace("etherscan.io", "explorer.cow.fi")
        embed = Embed(description=f"[cow explorer]({url})")
        await interaction.followup.send(embed=embed)

    async def _get_new_events(self) -> list[Event]:
        from_block = self.last_served_block + 1 - self.lookback_distance
        return await self.get_past_events(BlockNumber(from_block), self._pending_block)

    async def get_past_events(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[Event]:
        await self._ensure_setup()
        assert self._settlement is not None
        assert self._trade_topic is not None
        assert self._tokens is not None

        logs = await w3.eth.get_logs(
            {
                "address": self._settlement.address,
                "topics": [self._trade_topic],
                "fromBlock": from_block,
                "toBlock": to_block,
            }
        )

        if not logs:
            return []

        # decode logs into Trade events
        trades: list[EventData] = [
            self._settlement.events.Trade().process_log(raw_log) for raw_log in logs
        ]

        # filter for RPL/rETH trades
        trades = [
            t
            for t in trades
            if t["args"]["sellToken"].lower() in self._tokens
            or t["args"]["buyToken"].lower() in self._tokens
        ]

        if not trades:
            return []

        # get prices for USD threshold
        rpl_ratio = solidity.to_float(await rp.call("rocketNetworkPrices.getRPLPrice"))
        reth_ratio = solidity.to_float(await rp.call("rocketTokenRETH.getExchangeRate"))
        eth_usdc_price = await rp.get_eth_usdc_price()
        rpl_price = rpl_ratio * eth_usdc_price
        reth_price = reth_ratio * eth_usdc_price

        events: list[Event] = []
        for trade in trades:
            args = cast(CoWTradeArgs, trade["args"])
            data: aDict[str, Any] = aDict({})

            data["cow_uid"] = f"0x{args['orderUid'].hex()}"
            data["cow_owner"] = w3.to_checksum_address(args["owner"])
            data["transactionHash"] = trade["transactionHash"].to_0x_hex()

            sell_token: str = args["sellToken"].lower()
            buy_token: str = args["buyToken"].lower()

            if sell_token in self._tokens:
                token = "reth" if sell_token == self._tokens[1] else "rpl"
                data["event_name"] = f"cow_order_sell_{token}"
                data["ourAmount"] = solidity.to_float(args["sellAmount"])
                other_address = w3.to_checksum_address(args["buyToken"])
                decimals = 18
                s = await rp.assemble_contract(name="ERC20", address=other_address)
                with contextlib.suppress(Exception):
                    decimals = await s.functions.decimals().call()
                data["otherAmount"] = solidity.to_float(args["buyAmount"], decimals)
            else:
                token = "reth" if buy_token == self._tokens[1] else "rpl"
                data["event_name"] = f"cow_order_buy_{token}"
                data["ourAmount"] = solidity.to_float(args["buyAmount"])
                other_address = w3.to_checksum_address(args["sellToken"])
                decimals = 18
                s = await rp.assemble_contract(name="ERC20", address=other_address)
                with contextlib.suppress(Exception):
                    decimals = await s.functions.decimals().call()
                data["otherAmount"] = solidity.to_float(args["sellAmount"], decimals)

            data["ratioAmount"] = data["otherAmount"] / data["ourAmount"]

            # skip trades under minimum value
            if ((token == "rpl") and (data["ourAmount"] * rpl_price < 10_000)) or (
                (token == "reth") and (data["ourAmount"] * reth_price < 100_000)
            ):
                continue

            try:
                data["otherToken"] = await s.functions.symbol().call()
            except Exception:
                data["otherToken"] = "UNKWN"
                if other_address == w3.to_checksum_address(
                    "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
                ):
                    data["otherToken"] = "ETH"

            data = await prepare_args(data)
            embed = await assemble(data)
            events.append(
                Event(
                    embed=embed,
                    topic="cow_trade",
                    block_number=BlockNumber(trade["blockNumber"]),
                    event_name=data["event_name"],
                    unique_id=f"cow_trade_{trade['transactionHash'].hex()}:{trade['logIndex']}",
                    transaction_index=trade["transactionIndex"],
                    event_index=trade["logIndex"],
                )
            )

        return events


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(CoWOrders(bot))
