import logging
from typing import Dict, Any

from web3.beacon import AsyncBeacon
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

from utils.cfg import cfg

log = logging.getLogger("shared_w3")
log.setLevel(cfg.log_level)

w3 = AsyncWeb3(AsyncHTTPProvider(cfg.execution_layer.endpoint.current, request_kwargs={'timeout': 60}))
w3_mainnet = w3

if cfg.rocketpool.chain != "mainnet":
    w3_mainnet = AsyncWeb3(AsyncHTTPProvider(cfg.execution_layer.endpoint.mainnet))

w3_archive = None
if cfg.execution_layer.endpoint.archive is not None:
    w3_archive = AsyncWeb3(AsyncHTTPProvider(cfg.execution_layer.endpoint.archive))


class Bacon(AsyncBeacon):
    async def get_validators_by_ids(self, state_id: str, ids: list[int]) -> Dict[str, Any]:
        id_str = ','.join(map(str, ids))
        return await self._async_make_get_request(
            f"/eth/v1/beacon/states/{state_id}/validators?id={id_str}"
        )

    async def get_sync_committee(self, epoch: int) -> Dict[str, Any]:
        return await self._async_make_get_request(
            f"/eth/v1/beacon/states/head/sync_committees?epoch={epoch}"
        )


bacon = Bacon(cfg.consensus_layer.endpoint)
