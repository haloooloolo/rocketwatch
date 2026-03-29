import logging

from aiocache import cached
from ens import AsyncENS
from eth_typing import ChecksumAddress

from utils.shared_w3 import w3_mainnet

log = logging.getLogger("rocketwatch.cached_ens")


class CachedEns:
    def __init__(self):
        self.ens = AsyncENS.from_web3(w3_mainnet)

    @cached(key_builder=lambda _, _self, address: address)
    async def get_name(self, address: ChecksumAddress) -> str | None:
        log.debug(f"Retrieving ENS name for {address}")
        try:
            return await self.ens.name(address)
        except Exception as e:
            log.warning(f"ENS name lookup failed for {address}: {e}")
            return None

    @cached(key_builder=lambda _, _self, name: name)
    async def resolve_name(self, name: str) -> ChecksumAddress | None:
        log.debug(f"Resolving ENS name {name}")
        try:
            return await self.ens.address(name)
        except Exception as e:
            log.warning(f"ENS address resolution failed for {name}: {e}")
            return None


ens = CachedEns()
