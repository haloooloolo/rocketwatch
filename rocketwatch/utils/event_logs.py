import logging
from typing import Any

from eth_typing import BlockNumber
from web3.contract.async_contract import AsyncContractEvent
from web3.types import EventData

log = logging.getLogger("rocketwatch.event_logs")


async def get_logs(
    event: AsyncContractEvent,
    from_block: BlockNumber,
    to_block: BlockNumber,
    arg_filters: dict[str, Any] | None = None,
) -> list[EventData]:
    log.debug(f"Fetching event logs in [{from_block}, {to_block}]")

    chunk_size = 50_000
    results: list[EventData] = []
    chunk_start = from_block
    while chunk_start <= to_block:
        chunk_end = min(chunk_start + chunk_size, to_block)
        chunk = await event.get_logs(
            from_block=chunk_start,
            to_block=chunk_end,
            argument_filters=arg_filters,
        )
        results.extend(chunk)
        chunk_start = BlockNumber(chunk_end + 1)

    return results
