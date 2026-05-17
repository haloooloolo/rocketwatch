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


class _W3Proxy:
    _instance: AsyncWeb3[Any] | None = None

    def _build(self) -> AsyncWeb3[Any]:
        return _get_web3(cfg.execution_layer.endpoint.current)

    def __getattr__(self, name: str) -> Any:
        if self._instance is None:
            object.__setattr__(self, "_instance", self._build())
        return getattr(self._instance, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_instance":
            object.__setattr__(self, name, value)
            return
        if self._instance is None:
            object.__setattr__(self, "_instance", self._build())
        setattr(self._instance, name, value)


class _W3MainnetProxy(_W3Proxy):
    def _build(self) -> AsyncWeb3[Any]:
        endpoint = (
            cfg.execution_layer.endpoint.mainnet or cfg.execution_layer.endpoint.current
        )
        return _get_web3(endpoint)


class _BaconProxy:
    _instance: Bacon | None = None

    def __getattr__(self, name: str) -> Any:
        if self._instance is None:
            object.__setattr__(
                self,
                "_instance",
                Bacon(cfg.consensus_layer.endpoint, request_timeout=60),
            )
        return getattr(self._instance, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_instance":
            object.__setattr__(self, name, value)
            return
        if self._instance is None:
            object.__setattr__(
                self,
                "_instance",
                Bacon(cfg.consensus_layer.endpoint, request_timeout=60),
            )
        setattr(self._instance, name, value)


w3 = _W3Proxy()
w3_mainnet = _W3MainnetProxy()
bacon = _BaconProxy()
