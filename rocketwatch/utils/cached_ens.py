import logging
from typing import Optional

from ens import AsyncENS
from eth_typing import ChecksumAddress

from utils.cfg import cfg
from utils.shared_w3 import w3_mainnet

log = logging.getLogger("cached_ens")
log.setLevel(cfg["log_level"])

_name_cache: dict[ChecksumAddress, Optional[str]] = {}
_address_cache: dict[str, Optional[ChecksumAddress]] = {}


class CachedEns:
    def __init__(self):
        self.ens = AsyncENS.from_web3(w3_mainnet)

    async def get_name(self, address: ChecksumAddress) -> Optional[str]:
        if address in _name_cache:
            return _name_cache[address]
        log.debug(f"Retrieving ENS name for {address}")
        name = await self.ens.name(address)
        _name_cache[address] = name
        return name

    async def resolve_name(self, name: str) -> Optional[ChecksumAddress]:
        if name in _address_cache:
            return _address_cache[name]
        log.debug(f"Resolving ENS name {name}")
        address = await self.ens.address(name)
        _address_cache[name] = address
        return address
