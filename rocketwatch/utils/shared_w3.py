import logging

import aiohttp
from web3.beacon import Beacon as Bacon
from aiohttp.web import HTTPError
from eth_typing import BlockIdentifier
from web3 import Web3, HTTPProvider
from web3.middleware import geth_poa_middleware

from utils.cfg import cfg
from utils.retry import retry_async

log = logging.getLogger("shared_w3")
log.setLevel(cfg["log_level"])

w3 = Web3(HTTPProvider(cfg['execution_layer.endpoint.current'], request_kwargs={'timeout': 60}))
mainnet_w3 = w3

if cfg['rocketpool.chain'] != "mainnet":
    mainnet_w3 = Web3(HTTPProvider(cfg['execution_layer.endpoint.mainnet']))
    w3.middleware_onion.inject(geth_poa_middleware, layer=0)

historical_w3 = None
if "archive" in cfg['execution_layer.endpoint'].keys():
    historical_w3 = Web3(HTTPProvider(cfg['execution_layer.endpoint.archive']))

class SuperBacon(Bacon):
    def __init__(self, base_url: str) -> None:
        super().__init__(base_url)
        timeout = aiohttp.ClientTimeout(sock_connect=3.05, total=20)
        self.async_session = aiohttp.ClientSession(raise_for_status=True, timeout=timeout)
        
    @retry_async(tries=3, exceptions=HTTPError, delay=0.5)
    async def _make_get_request_async(self, url: str):
        async with self.async_session.get(url) as response:
            return await response.json()
        
    async def get_block_header_async(self, block_id: BlockIdentifier):
        url = f"{self.base_url}/eth/v1/beacon/headers/{block_id}"
        return await self._make_get_request_async(url)

    async def get_block_async(self, block_id: BlockIdentifier):
        url = f"{self.base_url}/eth/v2/beacon/blocks/{block_id}"
        return await self._make_get_request_async(url)

    async def get_validators_async(self, state_id, ids: list[int]):
        id_str = ','.join([str(i) for i in ids])
        url = f"{self.base_url}/eth/v1/beacon/states/{state_id}/validators?id={id_str}"
        return await self._make_get_request_async(url)
    
    async def get_sync_committee_async(self, epoch):
        url = f"{self.base_url}/eth/v1/beacon/states/head/sync_committees?epoch={epoch}"
        return await self._make_get_request_async(url)

bacon = SuperBacon(cfg["consensus_layer.endpoint"])
