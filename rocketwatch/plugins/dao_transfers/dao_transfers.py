"""Track ERC20 transfers sent from DAO multisig addresses."""

from __future__ import annotations

import contextlib
import logging
from typing import cast

from eth_abi.abi import encode
from eth_typing import BlockNumber, HexStr
from web3.types import FilterParams, LogReceipt

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import (
    build_small_event_embed,
    el_explorer_url,
    format_value,
)
from rocketwatch.utils.event import Event, EventPlugin
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.shared_w3 import w3

log = logging.getLogger("rocketwatch.dao_transfers")

_TRANSFER_TOPIC = w3.keccak(text="Transfer(address,address,uint256)").hex()


def _pad_address(addr: str) -> str:
    """Left-pad a 20-byte address to a 32-byte hex topic."""
    return "0x" + encode(["address"], [addr]).hex()


class DAOTransfers(EventPlugin):
    def __init__(self, bot: RocketWatch) -> None:
        super().__init__(bot)
        self._from_topics = [
            _pad_address(addr) for addr in cfg.rocketpool.dao_multisigs
        ]

    async def _get_new_events(self) -> list[Event]:
        from_block = self.last_served_block + 1 - self.lookback_distance
        return await self.get_past_events(BlockNumber(from_block), self._pending_block)

    async def get_past_events(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[Event]:
        if not self._from_topics:
            return []

        raw_logs: list[LogReceipt] = list(
            await w3.eth.get_logs(
                cast(
                    FilterParams,
                    {
                        "topics": [_TRANSFER_TOPIC, self._from_topics],
                        "fromBlock": from_block,
                        "toBlock": to_block,
                    },
                )
            )
        )

        if not raw_logs:
            return []

        events: list[Event] = []
        for raw_log in raw_logs:
            topics = raw_log["topics"]
            # topic1 = from (indexed), topic2 = to (indexed)
            from_addr = w3.to_checksum_address(topics[1][-20:])
            to_addr = w3.to_checksum_address(topics[2][-20:])
            value = int.from_bytes(raw_log["data"][-32:], "big")

            token_addr = raw_log["address"]
            erc20 = await rp.assemble_contract(name="ERC20", address=token_addr)

            symbol = "UNKNOWN"
            decimals = 18
            with contextlib.suppress(Exception):
                symbol = await erc20.functions.symbol().call()
            with contextlib.suppress(Exception):
                decimals = await erc20.functions.decimals().call()

            amount = value / 10**decimals

            from_url = await el_explorer_url(from_addr)
            to_url = await el_explorer_url(to_addr)
            tx_hash = HexStr(raw_log["transactionHash"].hex())

            embed = await build_small_event_embed(
                f":moneybag: {from_url} transferred "
                f"**{format_value(amount)} {symbol}** to {to_url}!",
                tx_hash,
            )

            events.append(
                Event(
                    embed=embed,
                    topic="events",
                    event_name="pdao_erc20_transfer_event",
                    unique_id=f"{tx_hash}:pdao_erc20_transfer:{raw_log['logIndex']}",
                    block_number=BlockNumber(raw_log["blockNumber"]),
                    transaction_index=raw_log["transactionIndex"],
                    event_index=raw_log["logIndex"],
                )
            )

        return events


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(DAOTransfers(bot))
