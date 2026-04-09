import logging
from typing import Any

from aiohttp import ClientTimeout
from eth_typing import URI
from web3 import AsyncWeb3
from web3.beacon import AsyncBeacon
from web3.providers import AsyncBaseProvider, AsyncHTTPProvider
from web3.types import RPCEndpoint, RPCResponse

from rocketwatch.utils.config import cfg

log = logging.getLogger("rocketwatch.shared_w3")


class Bacon(AsyncBeacon):
    _fallback_urls: list[str]

    def __init__(self, base_url: list[str], request_timeout: float = 10.0) -> None:
        self._fallback_urls = base_url
        super().__init__(base_url[0], request_timeout=request_timeout)

    async def _async_make_get_request(
        self,
        endpoint_uri: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        last_exc: Exception = RuntimeError("no fallback URLs configured")
        for url in self._fallback_urls:
            try:
                uri = URI(url + endpoint_uri)
                return await self._request_session_manager.async_json_make_get_request(
                    uri, params=params, timeout=ClientTimeout(self.request_timeout)
                )
            except Exception as exc:
                log.warning("Beacon %s failed: %s", url, exc)
                last_exc = exc
        raise last_exc

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


class AsyncFallbackProvider(AsyncBaseProvider):
    def __init__(self, providers: list[AsyncHTTPProvider]) -> None:
        super().__init__()
        self._providers = providers

    async def make_request(self, method: RPCEndpoint, params: Any) -> RPCResponse:
        last_exc: Exception = RuntimeError("no fallback providers configured")
        for provider in self._providers:
            try:
                return await provider.make_request(method, params)
            except Exception as exc:
                log.warning("Provider %s failed: %s", provider, exc)
                last_exc = exc
        raise last_exc

    async def is_connected(self, show_traceback: bool = False) -> bool:
        for provider in self._providers:
            if await provider.is_connected(show_traceback=show_traceback):
                return True
        return False


def _get_web3(endpoint: list[str]) -> AsyncWeb3[Any]:
    providers = [
        AsyncHTTPProvider(ep, request_kwargs={"timeout": 60}) for ep in endpoint
    ]
    return AsyncWeb3(AsyncFallbackProvider(providers))


w3 = _get_web3(cfg.execution_layer.endpoint.current)
w3_mainnet = w3

if cfg.execution_layer.endpoint.mainnet:
    w3_mainnet = _get_web3(cfg.execution_layer.endpoint.mainnet)

bacon = Bacon(cfg.consensus_layer.endpoint, request_timeout=60)
