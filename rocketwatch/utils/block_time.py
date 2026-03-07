import logging
import math

from aiocache import cached

from utils.cfg import cfg
from utils.shared_w3 import w3

log = logging.getLogger("block_time")
log.setLevel(cfg.log_level)


@cached()
async def block_to_ts(block_number: int) -> int:
    return (await w3.eth.get_block(block_number)).timestamp


async def ts_to_block(target_ts: int) -> int:
    log.debug(f"Looking for block at timestamp {target_ts}")
    if target_ts < await block_to_ts(1):
        # genesis block doesn't have a timestamp
        return 0

    lo = 1
    hi = await w3.eth.get_block_number() - 1

    # simple binary search over block numbers
    while lo < hi:
        mid = math.ceil((lo + hi) / 2)
        ts = await block_to_ts(mid)

        if ts < target_ts:
            lo = mid
        elif ts > target_ts:
            hi = mid - 1
        elif ts == target_ts:
            log.debug(f"Exact match: block {mid} @ {ts}")
            return mid

    # l == r, highest block number below target
    block = hi
    if abs(await block_to_ts(block + 1) - target_ts) < abs(await block_to_ts(block) - target_ts):
        block += 1

    log.debug(f"Closest match: block {block} @ {await block_to_ts(block)}")
    return block
