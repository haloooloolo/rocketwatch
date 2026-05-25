"""Tests for the fallback Beacon/RPC providers and the lazy w3/bacon proxies.

Everything chain-touching is mocked: we exercise the fallback iteration logic,
endpoint formatting, and the proxies' lazy-build-and-delegate behaviour.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from web3 import AsyncWeb3
from web3.types import RPCEndpoint

from rocketwatch.utils import shared_w3 as sw
from rocketwatch.utils.config import cfg


class TestBacon:
    async def test_fallback_tries_next_url_on_failure(self) -> None:
        bacon = sw.Bacon(["http://a", "http://b"])
        sm = MagicMock()
        sm.async_json_make_get_request = AsyncMock(
            side_effect=[RuntimeError("down"), {"ok": True}]
        )
        bacon._request_session_manager = sm

        result = await bacon._async_make_get_request("/x")

        assert result == {"ok": True}
        assert sm.async_json_make_get_request.await_count == 2

    async def test_all_urls_failing_raises_last(self) -> None:
        bacon = sw.Bacon(["http://a", "http://b"])
        sm = MagicMock()
        sm.async_json_make_get_request = AsyncMock(side_effect=RuntimeError("down"))
        bacon._request_session_manager = sm

        with pytest.raises(RuntimeError):
            await bacon._async_make_get_request("/x")

    async def test_get_validators_by_ids_formats_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bacon = sw.Bacon(["http://a"])
        captured = AsyncMock(return_value={"data": []})
        monkeypatch.setattr(bacon, "_async_make_get_request", captured)

        await bacon.get_validators_by_ids("head", [1, 2, 3])

        assert captured.await_args is not None
        endpoint = captured.await_args.args[0]
        assert "states/head/validators" in endpoint
        assert "id=1,2,3" in endpoint

    async def test_get_sync_committee_formats_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bacon = sw.Bacon(["http://a"])
        captured = AsyncMock(return_value={"data": {}})
        monkeypatch.setattr(bacon, "_async_make_get_request", captured)

        await bacon.get_sync_committee(42)

        assert captured.await_args is not None
        assert "sync_committees?epoch=42" in captured.await_args.args[0]


class TestAsyncFallbackProvider:
    def _provider(self, **kwargs: Any) -> MagicMock:
        provider = MagicMock()
        for name, value in kwargs.items():
            setattr(provider, name, value)
        return provider

    async def test_make_request_uses_next_provider_on_failure(self) -> None:
        p1 = self._provider(make_request=AsyncMock(side_effect=RuntimeError("x")))
        p2 = self._provider(make_request=AsyncMock(return_value={"result": 1}))
        providers: list[Any] = [p1, p2]

        fp = sw.AsyncFallbackProvider(providers)
        result = await fp.make_request(RPCEndpoint("eth_call"), [])

        assert result == {"result": 1}
        p1.make_request.assert_awaited_once()

    async def test_make_request_all_fail_raises(self) -> None:
        p1 = self._provider(make_request=AsyncMock(side_effect=RuntimeError("x")))
        providers: list[Any] = [p1]

        fp = sw.AsyncFallbackProvider(providers)
        with pytest.raises(RuntimeError):
            await fp.make_request(RPCEndpoint("eth_call"), [])

    async def test_is_connected_true_if_any_connected(self) -> None:
        p1 = self._provider(is_connected=AsyncMock(return_value=False))
        p2 = self._provider(is_connected=AsyncMock(return_value=True))
        providers: list[Any] = [p1, p2]

        assert await sw.AsyncFallbackProvider(providers).is_connected() is True

    async def test_is_connected_false_if_none_connected(self) -> None:
        p1 = self._provider(is_connected=AsyncMock(return_value=False))
        providers: list[Any] = [p1]

        assert await sw.AsyncFallbackProvider(providers).is_connected() is False


class TestGetWeb3:
    def test_builds_async_web3(self) -> None:
        assert isinstance(sw._get_web3(["http://localhost"]), AsyncWeb3)


class TestProxies:
    def test_w3_proxy_lazy_builds_and_delegates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = MagicMock()
        built.eth = "ETH"
        monkeypatch.setattr(sw, "_get_web3", lambda endpoint: built)
        proxy = sw._W3Proxy()
        proxy._instance = None

        assert proxy.eth == "ETH"
        # second access reuses the built instance (no rebuild)
        assert proxy.eth == "ETH"

    def test_w3_proxy_setattr_builds_then_delegates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = MagicMock()
        monkeypatch.setattr(sw, "_get_web3", lambda endpoint: built)
        proxy = sw._W3Proxy()
        proxy._instance = None

        proxy.middleware = "X"
        proxy.other = "Y"  # second set reuses the built instance

        assert built.middleware == "X"
        assert built.other == "Y"

    def test_mainnet_proxy_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        built = MagicMock()
        built.eth = "MAINNET"
        monkeypatch.setattr(sw, "_get_web3", lambda endpoint: built)
        proxy = sw._W3MainnetProxy()
        proxy._instance = None

        assert proxy.eth == "MAINNET"

    def test_bacon_proxy_lazy_builds(self) -> None:
        proxy = sw._BaconProxy()
        proxy._instance = None
        # __getattr__ builds a real Bacon from cfg.consensus_layer.endpoint
        assert proxy._fallback_urls == cfg.consensus_layer.endpoint
        # second access reuses the built instance
        assert proxy._fallback_urls == cfg.consensus_layer.endpoint

    def test_bacon_proxy_setattr_builds(self) -> None:
        proxy = sw._BaconProxy()
        proxy._instance = None

        proxy.request_timeout = 5
        proxy.request_timeout = 6  # second set reuses the built instance

        assert proxy._instance is not None
        assert proxy._instance.request_timeout == 6
