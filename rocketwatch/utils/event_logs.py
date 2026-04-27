import logging
from typing import Any

from eth_typing import BlockNumber
from web3.contract.async_contract import AsyncContractEvent
from web3.types import EventData

from rocketwatch.utils.shared_w3 import w3

log = logging.getLogger("rocketwatch.event_logs")


async def get_logs(
    event: AsyncContractEvent,
    from_block: BlockNumber,
    to_block: BlockNumber,
    arg_filters: dict[str, Any] | None = None,
    address_agnostic: bool = False,
) -> list[EventData]:
    """Fetch decoded logs for ``event`` over [from_block, to_block] in chunks.

    When ``address_agnostic`` is True, the filter matches only on the event
    topic — useful when the contract may have been redeployed over the scanned
    range and emissions from earlier addresses must still be captured.
    ``arg_filters`` is not supported in address-agnostic mode.
    """
    if address_agnostic and arg_filters:
        raise ValueError("arg_filters is not supported with address_agnostic=True")

    log.debug(
        f"Fetching event logs in [{from_block}, {to_block}] "
        f"(address_agnostic={address_agnostic})"
    )

    chunk_size = 50_000
    results: list[EventData] = []
    chunk_start = from_block
    while chunk_start <= to_block:
        chunk_end = min(chunk_start + chunk_size, to_block)
        if address_agnostic:
            raw_logs = await w3.eth.get_logs(
                {
                    "topics": [event.topic],
                    "fromBlock": chunk_start,
                    "toBlock": chunk_end,
                }
            )
            results.extend(event.process_log(entry) for entry in raw_logs)
        else:
            results.extend(
                await event.get_logs(
                    from_block=chunk_start,
                    to_block=chunk_end,
                    argument_filters=arg_filters,
                )
            )
        chunk_start = BlockNumber(chunk_end + 1)

    return results
