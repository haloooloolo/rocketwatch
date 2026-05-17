import logging
from functools import cache

from aiocache import cached
from ens import AsyncENS
from eth_typing import ChecksumAddress

from rocketwatch.utils.shared_w3 import w3_mainnet

log = logging.getLogger("rocketwatch.ens")


@cache
def _client() -> AsyncENS:
    return AsyncENS.from_web3(w3_mainnet)


@cached(key_builder=lambda _, address: address)
async def get_name(address: ChecksumAddress) -> str | None:
    log.debug(f"Retrieving ENS name for {address}")
    try:
        result: str | None = await _client().name(address)
        return result
    except Exception as e:
        log.warning(f"ENS name lookup failed for {address}: {e}")
        return None


@cached(key_builder=lambda _, name: name)
async def resolve_name(name: str) -> ChecksumAddress | None:
    log.debug(f"Resolving ENS name {name}")
    try:
        result: ChecksumAddress | None = await _client().address(name)
        return result
    except Exception as e:
        log.warning(f"ENS address resolution failed for {name}: {e}")
        return None
