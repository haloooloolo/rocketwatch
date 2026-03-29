from typing import Any

from web3 import AsyncWeb3
from web3.beacon import AsyncBeacon
from web3.providers import AsyncHTTPProvider

from utils.config import cfg


class Bacon(AsyncBeacon):
    async def get_validators_by_ids(
        self, state_id: str, ids: list[int]
    ) -> dict[str, Any]:
        id_str = ",".join(map(str, ids))
        return await self._async_make_get_request(
            f"/eth/v1/beacon/states/{state_id}/validators?id={id_str}"
        )

    async def get_sync_committee(self, epoch: int) -> dict[str, Any]:
        return await self._async_make_get_request(
            f"/eth/v1/beacon/states/head/sync_committees?epoch={epoch}"
        )


def _get_web3(endpoint: str) -> AsyncWeb3:
    provider = AsyncHTTPProvider(endpoint, request_kwargs={"timeout": 60})
    return AsyncWeb3(provider)


w3 = _get_web3(cfg.execution_layer.endpoint.current)
w3_mainnet = w3
w3_archive = w3

if cfg.rocketpool.chain.lower() != "mainnet":
    w3_mainnet = _get_web3(cfg.execution_layer.endpoint.mainnet)

if cfg.execution_layer.endpoint.archive is not None:
    w3_archive = _get_web3(cfg.execution_layer.endpoint.archive)

bacon = Bacon(cfg.consensus_layer.endpoint, request_timeout=60)
