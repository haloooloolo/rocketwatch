import logging
from typing import Dict, Any

from web3.beacon import AsyncBeacon
from web3 import Web3, AsyncWeb3, HTTPProvider
from web3.providers import AsyncHTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware

from utils.cfg import cfg

log = logging.getLogger("shared_w3")
log.setLevel(cfg["log_level"])

w3 = Web3(HTTPProvider(cfg['execution_layer.endpoint.current'], request_kwargs={'timeout': 60}))
w3_async = AsyncWeb3(AsyncHTTPProvider(cfg['execution_layer.endpoint.current'], request_kwargs={'timeout': 60}))
mainnet_w3 = w3

if cfg['rocketpool.chain'] != "mainnet":
    mainnet_w3 = Web3(HTTPProvider(cfg['execution_layer.endpoint.mainnet']))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    w3_async.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

historical_w3 = None
if "archive" in cfg['execution_layer.endpoint'].keys():
    historical_w3 = Web3(HTTPProvider(cfg['execution_layer.endpoint.archive']))


class Bacon(AsyncBeacon):
    async def get_validators_by_ids(self, state_id: str, ids: list[int]) -> Dict[str, Any]:
        id_str = ','.join(map(str, ids))
        return await self._async_make_get_request(
            f"/eth/v1/beacon/states/{state_id}/validators?id={id_str}"
        )

bacon = Bacon(cfg["consensus_layer.endpoint"])
