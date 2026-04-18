import logging
import math
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

import aiohttp
import numpy as np
from eth_typing import ChecksumAddress, HexStr
from web3.contract import AsyncContract
from web3.contract.async_contract import AsyncContractFunction

from rocketwatch.utils.retry import retry
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.shared_w3 import w3

log = logging.getLogger("rocketwatch.liquidity")


class Liquidity:
    def __init__(self, price: float, depth_fn: Callable[[float], float]):
        self.price = price
        self.__depth_fn = depth_fn

    def depth_at(self, price: float) -> float:
        return self.__depth_fn(price)


class Exchange(ABC):
    def __str__(self) -> str:
        return self.__class__.__name__

    @property
    @abstractmethod
    def color(self) -> str:
        pass


@dataclass(frozen=True, slots=True)
class Market:
    major: str
    minor: str


class CEX(Exchange, ABC):
    def __init__(self, major: str, minors: list[str]):
        self.markets = {Market(major.upper(), minor.upper()) for minor in minors}

    @property
    @abstractmethod
    def _api_base_url(self) -> str:
        pass

    @staticmethod
    @abstractmethod
    def _get_request_path(market: Market) -> str:
        pass

    @staticmethod
    @abstractmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        pass

    @abstractmethod
    def _get_bids(self, api_response: Any) -> dict[float, float]:
        """Extract mapping of price to major-denominated bid liquidity from API response"""
        pass

    @abstractmethod
    def _get_asks(self, api_response: Any) -> dict[float, float]:
        """Extract mapping of price to major-denominated ask liquidity from API response"""
        pass

    @retry(tries=3, delay=1)
    async def _get_order_book(
        self, market: Market, session: aiohttp.ClientSession
    ) -> tuple[dict[float, float], dict[float, float]]:
        params = self._get_request_params(market)
        url = self._api_base_url + self._get_request_path(market)
        response = await session.get(
            url, params=params, headers={"User-Agent": "Rocket Watch"}
        )
        log.debug(f"response from {url}: {response}")
        data = await response.json()
        bids = OrderedDict(sorted(self._get_bids(data).items(), reverse=True))
        asks = OrderedDict(sorted(self._get_asks(data).items()))
        return bids, asks

    async def _get_liquidity(
        self, market: Market, session: aiohttp.ClientSession
    ) -> Liquidity | None:
        bids, asks = await self._get_order_book(market, session)
        if not (bids and asks):
            log.warning("Empty order book")
            return None

        bid_prices = np.array(list(bids.keys()))
        bid_liquidity = np.cumsum([p * bids[p] for p in bids])

        ask_prices = np.array(list(asks.keys()))
        ask_liquidity = np.cumsum([p * asks[p] for p in asks])

        max_bid = float(bid_prices[0])
        min_ask = float(ask_prices[0])
        price = (max_bid + min_ask) / 2

        def depth_at(_price: float) -> float:
            if max_bid < _price < min_ask:
                return 0

            if _price <= max_bid:
                i = int(np.searchsorted(-bid_prices, -_price, "right"))
                return float(bid_liquidity[min(i, len(bid_liquidity)) - 1])
            else:
                i = int(np.searchsorted(ask_prices, _price, "right"))
                return float(ask_liquidity[min(i, len(ask_liquidity)) - 1])

        return Liquidity(price, depth_at)

    async def get_liquidity(
        self, session: aiohttp.ClientSession
    ) -> dict[Market, Liquidity]:
        markets = {}
        for market in self.markets:
            if liq := await self._get_liquidity(market, session):
                markets[market] = liq
        return markets


class Binance(CEX):
    @property
    def color(self) -> str:
        return "#E6B800"

    @property
    def _api_base_url(self) -> str:
        return "https://api.binance.com/api/v3"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/depth"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"symbol": f"{market.major}{market.minor}", "limit": 5000}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {float(price): float(size) for price, size in api_response["bids"]}

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {float(price): float(size) for price, size in api_response["asks"]}


class Coinbase(CEX):
    @property
    def color(self) -> str:
        return "#0B3EF4"

    @property
    def _api_base_url(self) -> str:
        return "https://api.coinbase.com/api/v3"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/brokerage/market/product_book"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"product_id": f"{market.major}-{market.minor}"}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(bid["price"]): float(bid["size"])
            for bid in api_response["pricebook"]["bids"]
        }

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(ask["price"]): float(ask["size"])
            for ask in api_response["pricebook"]["asks"]
        }


class Deepcoin(CEX):
    @property
    def color(self) -> str:
        return "#D36F3F"

    @property
    def _api_base_url(self) -> str:
        return "https://api.deepcoin.com/deepcoin"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/market/books"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"instId": f"{market.major}-{market.minor}", "sz": 400}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size) for price, size in api_response["data"]["bids"]
        }

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size) for price, size in api_response["data"]["asks"]
        }


class GateIO(CEX):
    @property
    def color(self) -> str:
        return "#00B383"

    @property
    def _api_base_url(self) -> str:
        return "https://api.gateio.ws/api/v4"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/spot/order_book"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"currency_pair": f"{market.major}_{market.minor}", "limit": 1000}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {float(price): float(size) for price, size in api_response["bids"]}

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {float(price): float(size) for price, size in api_response["asks"]}


class OKX(CEX):
    @property
    def color(self) -> str:
        return "#080808"

    @property
    def _api_base_url(self) -> str:
        return "https://www.okx.com/api/v5"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/market/books"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"instId": f"{market.major}-{market.minor}", "sz": 400}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size)
            for price, size, _, _ in api_response["data"][0]["bids"]
        }

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size)
            for price, size, _, _ in api_response["data"][0]["asks"]
        }


class Bitget(CEX):
    @property
    def color(self) -> str:
        return "#00C1D6"

    @property
    def _api_base_url(self) -> str:
        return "https://api.bitget.com/api/v2"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/spot/market/orderbook"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"symbol": f"{market.major}{market.minor}", "limit": 150}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size) for price, size in api_response["data"]["bids"]
        }

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size) for price, size in api_response["data"]["asks"]
        }


class MEXC(CEX):
    @property
    def color(self) -> str:
        return "#003366"

    @property
    def _api_base_url(self) -> str:
        return "https://api.mexc.com/api/v3"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/depth"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"symbol": f"{market.major}{market.minor}", "limit": 5000}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {float(price): float(size) for price, size in api_response["bids"]}

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {float(price): float(size) for price, size in api_response["asks"]}


class Bybit(CEX):
    @property
    def color(self) -> str:
        return "#E89C20"

    @property
    def _api_base_url(self) -> str:
        return "https://api.bybit.com/v5"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/market/orderbook"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {
            "category": "spot",
            "symbol": f"{market.major}{market.minor}",
            "limit": 200,
        }

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size) for price, size in api_response["result"]["b"]
        }

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size) for price, size in api_response["result"]["a"]
        }


class CryptoDotCom(CEX):
    def __str__(self) -> str:
        return "Crypto.com"

    @property
    def color(self) -> str:
        return "#172B4D"

    @property
    def _api_base_url(self) -> str:
        return "https://api.crypto.com/exchange/v1/public"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/get-book"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"instrument_name": f"{market.major}_{market.minor}", "depth": 150}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size)
            for price, size, _ in api_response["result"]["data"][0]["bids"]
        }

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size)
            for price, size, _ in api_response["result"]["data"][0]["asks"]
        }


class Kraken(CEX):
    @property
    def color(self) -> str:
        return "#8055E5"

    @property
    def _api_base_url(self) -> str:
        return "https://api.kraken.com/0/public"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/Depth"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"pair": f"{market.major}{market.minor}", "count": 500}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size)
            for price, size, _ in next(iter(api_response["result"].values()))["bids"]
        }

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size)
            for price, size, _ in next(iter(api_response["result"].values()))["asks"]
        }


class Kucoin(CEX):
    @property
    def color(self) -> str:
        return "#2E8B57"

    @property
    def _api_base_url(self) -> str:
        return "https://api.kucoin.com/api/v1"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/market/orderbook/level2_100"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"symbol": f"{market.major}-{market.minor}"}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size) for price, size in api_response["data"]["bids"]
        }

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size) for price, size in api_response["data"]["asks"]
        }


class Bithumb(CEX):
    @property
    def color(self) -> str:
        return "#E36200"

    @property
    def _api_base_url(self) -> str:
        return "https://api.bithumb.com/v1"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/orderbook"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"markets": f"{market.minor}-{market.major}"}

    def _get_bids(self, api_response: list[dict[str, Any]]) -> dict[float, float]:
        return {
            entry["bid_price"]: entry["bid_size"]
            for entry in api_response[0]["orderbook_units"]
        }

    def _get_asks(self, api_response: list[dict[str, Any]]) -> dict[float, float]:
        return {
            entry["ask_price"]: entry["ask_size"]
            for entry in api_response[0]["orderbook_units"]
        }


class BingX(CEX):
    @property
    def color(self) -> str:
        return "#0084D6"

    @property
    def _api_base_url(self) -> str:
        return "https://open-api.bingx.com/openApi/spot/v1"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/market/depth"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"symbol": f"{market.major}-{market.minor}", "limit": 1000}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size) for price, size in api_response["data"]["bids"]
        }

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size) for price, size in api_response["data"]["asks"]
        }


class Bitvavo(CEX):
    @property
    def color(self) -> str:
        return "#2323C2"

    @property
    def _api_base_url(self) -> str:
        return "https://api.bitvavo.com/v2"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return f"/{market.major}-{market.minor}/book"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"depth": 1000}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {float(price): float(size) for price, size in api_response["bids"]}

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {float(price): float(size) for price, size in api_response["asks"]}


class HTX(CEX):
    @property
    def color(self) -> str:
        return "#297BBF"

    @property
    def _api_base_url(self) -> str:
        return "https://api.huobi.pro"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/market/depth"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {
            "symbol": f"{market.major.lower()}{market.minor.lower()}",
            "type": "step0",
        }

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(entry[0]): float(entry[1]) for entry in api_response["tick"]["bids"]
        }

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(entry[0]): float(entry[1]) for entry in api_response["tick"]["asks"]
        }


class BitMart(CEX):
    @property
    def color(self) -> str:
        return "#19C39C"

    @property
    def _api_base_url(self) -> str:
        return "https://api-cloud.bitmart.com"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/spot/quotation/v3/books"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"symbol": f"{market.major}_{market.minor}", "limit": 50}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(entry[0]): float(entry[1]) for entry in api_response["data"]["bids"]
        }

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(entry[0]): float(entry[1]) for entry in api_response["data"]["asks"]
        }


class Bitrue(CEX):
    @property
    def color(self) -> str:
        return "#C5972D"

    @property
    def _api_base_url(self) -> str:
        return "https://b.bitrue.com/kline-api"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/depths"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"symbol": f"{market.major}{market.minor}"}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(entry[0]): float(entry[1])
            for entry in api_response["data"]["tick"]["b"]
        }

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(entry[0]): float(entry[1])
            for entry in api_response["data"]["tick"]["a"]
        }


class CoinTR(CEX):
    @property
    def color(self) -> str:
        return "#42A036"

    @property
    def _api_base_url(self) -> str:
        return "https://api.cointr.com/api/v2"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/spot/market/orderbook"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"symbol": f"{market.major}{market.minor}", "limit": 150}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size) for price, size in api_response["data"]["bids"]
        }

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {
            float(price): float(size) for price, size in api_response["data"]["asks"]
        }


class DigiFinex(CEX):
    @property
    def color(self) -> str:
        return "#5E4EB3"

    @property
    def _api_base_url(self) -> str:
        return "https://openapi.digifinex.com/v3"

    @staticmethod
    def _get_request_path(market: Market) -> str:
        return "/order_book"

    @staticmethod
    def _get_request_params(market: Market) -> dict[str, str | int]:
        return {"symbol": f"{market.major}_{market.minor}", "limit": 150}

    def _get_bids(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {price: size for price, size in api_response["bids"]}

    def _get_asks(self, api_response: dict[str, Any]) -> dict[float, float]:
        return {price: size for price, size in api_response["bids"]}


class ERC20Token:
    def __init__(self, address: ChecksumAddress, symbol: str, decimals: int):
        self.address = address
        self.symbol = symbol
        self.decimals = decimals

    @classmethod
    async def create(cls, address: ChecksumAddress) -> "ERC20Token":
        address = w3.to_checksum_address(address)
        if int(address, 16) == 0:
            # native ETH (Uniswap V4 convention)
            return cls(address, "ETH", 18)
        contract = await rp.assemble_contract("ERC20", address, mainnet=True)
        symbol, decimals = await rp.multicall(
            [contract.functions.symbol(), contract.functions.decimals()]
        )
        return cls(address, symbol, decimals)

    def __str__(self) -> str:
        return self.symbol

    def __repr__(self) -> str:
        return f"{self.symbol} ({self.address})"


class DEX(Exchange, ABC):
    class LiquidityPool(ABC):
        @abstractmethod
        async def get_price(self) -> float:
            pass

        @abstractmethod
        async def get_normalized_price(self) -> float:
            pass

        @abstractmethod
        async def get_liquidity(self) -> Liquidity | None:
            pass

    def __init__(self, pools: Sequence[LiquidityPool]):
        self.pools = pools

    async def get_liquidity(self) -> dict[LiquidityPool, Liquidity]:
        pools = {}
        for pool in self.pools:
            if liq := await pool.get_liquidity():
                pools[pool] = liq
        return pools


class BalancerV2(DEX):
    class WeightedPool(DEX.LiquidityPool):
        def __init__(
            self,
            pool_id: HexStr,
            vault: AsyncContract,
            token_0: ERC20Token,
            token_1: ERC20Token,
        ):
            self.id = pool_id
            self.vault = vault
            self.token_0 = token_0
            self.token_1 = token_1

        @classmethod
        async def create(cls, pool_id: HexStr) -> "BalancerV2.WeightedPool":
            vault = await rp.get_contract_by_name("BalancerV2Vault", mainnet=True)
            tokens = (await vault.functions.getPoolTokens(pool_id).call())[0]
            token_0 = await ERC20Token.create(tokens[0])
            token_1 = await ERC20Token.create(tokens[1])
            return cls(pool_id, vault, token_0, token_1)

        async def get_price(self) -> float:
            balances = (await self.vault.functions.getPoolTokens(self.id).call())[1]
            return balances[1] / balances[0] if (balances[0] > 0) else 0

        async def get_normalized_price(self) -> float:
            exponent: int = self.token_0.decimals - self.token_1.decimals
            return float(await self.get_price() * (10**exponent))

        async def get_liquidity(self) -> Liquidity | None:
            balance_0, balance_1 = (
                await self.vault.functions.getPoolTokens(self.id).call()
            )[1]
            if (balance_0 == 0) or (balance_1 == 0):
                log.warning("Empty token balances")
                return None

            balance_norm = 10 ** (self.token_1.decimals - self.token_0.decimals)
            price = balance_norm * balance_0 / balance_1

            # assume equal weights and liquidity in token 0 for now
            def depth_at(_price: float) -> float:
                invariant = balance_0 * balance_1
                new_balance_0 = math.sqrt(_price * invariant / balance_norm)
                return float(
                    abs(new_balance_0 - balance_0) / (10**self.token_0.decimals)
                )

            return Liquidity(price, depth_at)

    class MetaStablePool(DEX.LiquidityPool):
        """Balancer V2 MetaStable pool with 2 tokens + rate providers.

        Uses Curve-style stableswap math on ``normalized`` balances, where each
        raw balance is multiplied by its rate-provider rate (e.g. rETH's
        ``getExchangeRate``). Solves for depth by bisecting on the new token_0
        balance whose post-swap spot price matches the target.
        """

        # Minimal ABI — we only need getAmplificationParameter on the pool
        _POOL_ABI: ClassVar[list[dict[str, Any]]] = [
            {
                "inputs": [],
                "name": "getAmplificationParameter",
                "outputs": [
                    {"name": "value", "type": "uint256"},
                    {"name": "isUpdating", "type": "bool"},
                    {"name": "precision", "type": "uint256"},
                ],
                "stateMutability": "view",
                "type": "function",
            }
        ]

        def __init__(
            self,
            pool_id: HexStr,
            vault: AsyncContract,
            pool_contract: AsyncContract,
            token_0: ERC20Token,
            token_1: ERC20Token,
            rate_fn_0: Callable[[], Any] | None,
            rate_fn_1: Callable[[], Any] | None,
            primary_is_token_0: bool = False,
        ):
            self.id = pool_id
            self.vault = vault
            self.pool_contract = pool_contract
            self.token_0 = token_0
            self.token_1 = token_1
            # Async callables returning real rate (e.g. 1.057 for rETH). None = 1.
            self.rate_fn_0 = rate_fn_0
            self.rate_fn_1 = rate_fn_1
            self.primary_is_token_0 = primary_is_token_0

        @classmethod
        async def create(
            cls,
            pool_id: HexStr,
            rate_fn_0: Callable[[], Any] | None = None,
            rate_fn_1: Callable[[], Any] | None = None,
            primary_is_token_0: bool = False,
        ) -> "BalancerV2.MetaStablePool":
            vault = await rp.get_contract_by_name("BalancerV2Vault", mainnet=True)
            tokens = (await vault.functions.getPoolTokens(pool_id).call())[0]
            pool_addr = w3.to_checksum_address(pool_id[:42])
            pool_contract = w3.eth.contract(address=pool_addr, abi=cls._POOL_ABI)
            token_0 = await ERC20Token.create(tokens[0])
            token_1 = await ERC20Token.create(tokens[1])
            return cls(
                pool_id,
                vault,
                pool_contract,
                token_0,
                token_1,
                rate_fn_0,
                rate_fn_1,
                primary_is_token_0,
            )

        @staticmethod
        def _compute_invariant(amp: float, x0: float, x1: float) -> float:
            """Newton iteration for n=2 stableswap invariant D (Balancer V2 form).

            D_P = D^(n+1) / (n^n * prod(x)); for n=2 that's D^3 / (4*x0*x1).
            D_new = (n*D + A*n*S) * D / (n*(1+A)*D - D_P)
            """
            S = x0 + x1
            if S == 0:
                return 0.0
            D = S
            for _ in range(255):
                D_prev = D
                D_P = (D**3) / (4 * x0 * x1)
                D = (2 * D + 2 * amp * S) * D / (2 * (1 + amp) * D - D_P)
                if abs(D - D_prev) < 1e-9:
                    break
            return D

        @staticmethod
        def _balance_given_invariant(amp: float, D: float, x_other: float) -> float:
            """Solve for token balance given the other balance and D (n=2 quadratic).

            Inverts the same fixed-point ``_compute_invariant`` converges to,
            ``D_P = 2*amp*(D - S)``; that rearranges to
            ``8*amp*x_other*x^2 + 8*amp*x_other*(x_other - D)*x + D^3 = 0``.
            Returns the larger (equilibrium-adjacent) positive root, or a tiny
            positive value when no real solution exists (x_other outside the
            feasible region). The rationalized form avoids catastrophic
            cancellation for large positive ``b``.
            """
            a = 8 * amp * x_other
            b = 8 * amp * x_other * (x_other - D)
            disc = b * b - 4 * a * (D**3)
            if disc <= 0:
                # Infeasible — no positive real root; caller's bisection will
                # steer away. Return a vanishing value to keep depth_at finite.
                return max(D - x_other, 1e-30)
            sqrt_disc = math.sqrt(disc)
            if b <= 0:
                return (-b + sqrt_disc) / (2 * a)
            # b > 0: -b + sqrt_disc cancels catastrophically. Use Vieta's:
            # x1 * x2 = c/a = D^3 / (8*amp*x_other), so the larger-in-magnitude
            # root is (-b - sqrt_disc)/(2a) (negative) and the smaller is
            # 2c / (-b - sqrt_disc) (also negative). Both are negative →
            # infeasible for positive balance.
            return max(D - x_other, 1e-30)

        @staticmethod
        def _spot_price(amp: float, D: float, x0: float, x1: float) -> float:
            """∂f/∂x0 / ∂f/∂x1 — raw t1 received per raw t0 input (infinitesimal)."""
            df0 = 4 * amp + D**3 / (4 * x0**2 * x1)
            df1 = 4 * amp + D**3 / (4 * x0 * x1**2)
            return df0 / df1

        async def _get_state(self) -> tuple[float, float, float, float, float]:
            """Return (N0, N1, amp, r0, r1) — normalized balances, amp, rates."""
            (_, balances, _), amp_tuple = await rp.multicall(
                [
                    self.vault.functions.getPoolTokens(self.id),
                    self.pool_contract.functions.getAmplificationParameter(),
                ]
            )
            amp = float(amp_tuple[0]) / float(amp_tuple[2])
            r0 = float(await self.rate_fn_0()) if self.rate_fn_0 else 1.0
            r1 = float(await self.rate_fn_1()) if self.rate_fn_1 else 1.0
            N0 = balances[0] / 10**self.token_0.decimals * r0
            N1 = balances[1] / 10**self.token_1.decimals * r1
            return N0, N1, amp, r0, r1

        async def get_price(self) -> float:
            N0, N1, amp, r0, r1 = await self._get_state()
            D = self._compute_invariant(amp, N0, N1)
            return self._spot_price(amp, D, N0, N1) * r0 / r1

        async def get_normalized_price(self) -> float:
            return await self.get_price()

        async def get_liquidity(self) -> Liquidity | None:
            N0, N1, amp, r0, r1 = await self._get_state()
            if N0 <= 0 or N1 <= 0:
                log.warning("Empty balances in MetaStable pool")
                return None

            D = self._compute_invariant(amp, N0, N1)
            spot_norm_0 = self._spot_price(amp, D, N0, N1)
            # Real swap rate (raw t1 per raw t0) = spot_norm * r0/r1
            primary_0 = self.primary_is_token_0

            def depth_at(_price: float) -> float:
                # _price is real "quote per primary" in base-ETH-equivalent units.
                # liq_price = (raw_spot or 1/raw_spot) * rate_quote, so inverting:
                #  primary=token_0 (real_price = raw_spot * r1):
                #      raw_spot = _price/r1, spot_norm = raw_spot * r1/r0 = _price/r0
                #  primary=token_1 (real_price = 1/raw_spot * r0):
                #      raw_spot = r0/_price, spot_norm = raw_spot * r1/r0 = r1/_price
                if _price <= 0:
                    return 0.0
                spot_target = _price / r0 if primary_0 else r1 / _price

                # Bisect on N0 (normalized token_0 balance): adding N0 → spot
                # drops. Feasibility of the invariant requires roughly N0 < D
                # (beyond that, no positive N1 satisfies the stableswap curve),
                # so cap the upper bound well below D. Likewise the lower bound
                # cannot reach 0 without N1 blowing up.
                if spot_target < spot_norm_0:
                    lo, hi = N0, min(N0 * 1e3, D * 0.999)
                elif spot_target > spot_norm_0:
                    lo, hi = max(N0 * 1e-3, D * 1e-6), N0
                else:
                    return 0.0

                for _ in range(80):
                    mid = (lo + hi) / 2
                    mid_N1 = BalancerV2.MetaStablePool._balance_given_invariant(
                        amp, D, mid
                    )
                    mid_spot = BalancerV2.MetaStablePool._spot_price(
                        amp, D, mid, mid_N1
                    )
                    if mid_spot < spot_target:
                        hi = mid
                    else:
                        lo = mid
                    if abs(hi - lo) / max(hi, 1.0) < 1e-12:
                        break

                mid_N0 = (lo + hi) / 2
                mid_N1 = BalancerV2.MetaStablePool._balance_given_invariant(
                    amp, D, mid_N0
                )
                # Return quote delta in "real base" units (normalized dN already
                # includes the quote rate, so this is base-ETH-equivalent for
                # a pool whose quote token has a non-trivial rate provider).
                return abs(mid_N1 - N1) if primary_0 else abs(mid_N0 - N0)

            raw_spot = spot_norm_0 * r0 / r1  # raw t1 per raw t0
            rate_quote = r1 if primary_0 else r0
            liq_price_raw = raw_spot if primary_0 else 1.0 / raw_spot
            return Liquidity(liq_price_raw * rate_quote, depth_at)

    def __init__(self, pools: list[DEX.LiquidityPool]):
        super().__init__(pools)

    def __str__(self) -> str:
        return "Balancer V2"

    @property
    def color(self) -> str:
        return "#C0C0C0"


class BalancerV3(DEX):
    """Balancer V3 — singleton vault (different address from V2), new stable pools.

    Reuses V2 MetaStablePool's stableswap math; only state fetching differs.
    """

    class StablePool(BalancerV2.MetaStablePool):
        def __init__(
            self,
            pool_address: ChecksumAddress,
            vault: AsyncContract,
            pool_contract: AsyncContract,
            token_0: ERC20Token,
            token_1: ERC20Token,
            primary_is_token_0: bool = False,
        ):
            # Bypass V2 __init__ since our state model differs (no pool_id, no
            # V2 vault, no rate callbacks — V3 vault returns pre-scaled balances)
            self.pool_address = pool_address
            self.vault = vault
            self.pool_contract = pool_contract
            self.token_0 = token_0
            self.token_1 = token_1
            self.primary_is_token_0 = primary_is_token_0

        @classmethod
        async def create(  # type: ignore[override]
            cls,
            pool_address: ChecksumAddress,
            primary_is_token_0: bool = False,
        ) -> "BalancerV3.StablePool":
            vault = await rp.get_contract_by_name("BalancerV3Vault", mainnet=True)
            pool_contract = w3.eth.contract(address=pool_address, abi=cls._POOL_ABI)
            tokens, _, _, _ = await vault.functions.getPoolTokenInfo(
                pool_address
            ).call()
            token_0 = await ERC20Token.create(tokens[0])
            token_1 = await ERC20Token.create(tokens[1])
            return cls(
                pool_address,
                vault,
                pool_contract,
                token_0,
                token_1,
                primary_is_token_0,
            )

        async def _get_state(
            self,
        ) -> tuple[float, float, float, float, float]:
            """Return (N0, N1, amp, r0, r1). V3 vault pre-scales balances, so we
            back out the rate from balancesRaw + lastBalancesLiveScaled18."""
            pool_info_call = self.vault.functions.getPoolTokenInfo(self.pool_address)
            amp_call = self.pool_contract.functions.getAmplificationParameter()
            pool_info, amp_tuple = await rp.multicall([pool_info_call, amp_call])
            _, _, balances_raw, balances_live = pool_info
            amp = float(amp_tuple[0]) / float(amp_tuple[2])

            # balancesLiveScaled18 = rawBalance * tokenScalingFactor * rate / 1e18,
            # where tokenScalingFactor = 10**(18 - decimals). Derive each rate.
            def _rate(raw: int, live: int, decimals: int) -> float:
                # Balancer V3: live_scaled18 = raw * 10^(18-decimals) * rate / 1e18;
                # rate is itself 1e18-scaled, so human rate = live / (raw * 10^(18-decimals)).
                if raw == 0:
                    return 1.0
                return float(live) / float(raw * 10 ** (18 - decimals))

            r0 = _rate(balances_raw[0], balances_live[0], self.token_0.decimals)
            r1 = _rate(balances_raw[1], balances_live[1], self.token_1.decimals)
            N0 = balances_live[0] / 1e18
            N1 = balances_live[1] / 1e18
            return N0, N1, amp, r0, r1

    def __init__(self, pools: list[StablePool]):
        super().__init__(pools)

    def __str__(self) -> str:
        return "Balancer V3"

    @property
    def color(self) -> str:
        return "#8F8F8F"


class UniswapV3(DEX):
    TICK_WORD_SIZE = 256
    MIN_TICK = -887_272
    MAX_TICK = 887_272

    @staticmethod
    def tick_to_price(tick: float) -> float:
        return float(1.0001**tick)

    @staticmethod
    def price_to_tick(price: float) -> float:
        return math.log(price, 1.0001)

    class Pool(DEX.LiquidityPool):
        def __init__(
            self,
            pool_address: ChecksumAddress,
            contract: AsyncContract,
            tick_spacing: int,
            token_0: ERC20Token,
            token_1: ERC20Token,
            primary_is_token_0: bool = False,
        ):
            self.pool_address = pool_address
            self.contract = contract
            self.tick_spacing = tick_spacing
            self.token_0 = token_0
            self.token_1 = token_1
            # When True, token_0 is the asset being priced; .price and depth_at
            # are in "token_1 per token_0" and depth is returned in token_1.
            # Default False matches Uniswap's address-ordering convention when
            # the primary asset happens to have the higher address.
            self.primary_is_token_0 = primary_is_token_0

        @classmethod
        async def create(
            cls,
            pool_address: ChecksumAddress,
            primary_is_token_0: bool = False,
        ) -> "UniswapV3.Pool":
            contract = await rp.assemble_contract(
                "UniswapV3Pool", pool_address, mainnet=True
            )
            tick_spacing, token_0_addr, token_1_addr = await rp.multicall(
                [
                    contract.functions.tickSpacing(),
                    contract.functions.token0(),
                    contract.functions.token1(),
                ]
            )
            token_0 = await ERC20Token.create(token_0_addr)
            token_1 = await ERC20Token.create(token_1_addr)
            return cls(
                pool_address,
                contract,
                tick_spacing,
                token_0,
                token_1,
                primary_is_token_0,
            )

        # on-chain read hooks — V4 overrides these to go through StateView
        def _fn_slot0(self) -> AsyncContractFunction:
            return self.contract.functions.slot0()

        def _fn_liquidity(self) -> AsyncContractFunction:
            return self.contract.functions.liquidity()

        def _fn_ticks(self, tick: int) -> AsyncContractFunction:
            return self.contract.functions.ticks(tick)

        def _fn_tick_bitmap(self, word: int) -> AsyncContractFunction:
            return self.contract.functions.tickBitmap(word)

        def tick_to_word_and_bit(self, tick: int) -> tuple[int, int]:
            compressed = int(tick // self.tick_spacing)
            if (tick < 0) and (tick % self.tick_spacing):
                compressed -= 1

            word_position = int(compressed // UniswapV3.TICK_WORD_SIZE)
            bit_position = compressed % UniswapV3.TICK_WORD_SIZE
            return word_position, bit_position

        async def get_ticks_net_liquidity(self, ticks: list[int]) -> dict[int, int]:
            results = await rp.multicall([self._fn_ticks(tick) for tick in ticks])
            return dict(zip(ticks, [r[1] for r in results], strict=False))

        async def get_initialized_ticks(self, current_tick: int) -> list[int]:
            ticks = []
            active_word, b = self.tick_to_word_and_bit(current_tick)

            word_range = list(range(active_word - 20, active_word + 20))
            bitmaps = await rp.multicall(
                [self._fn_tick_bitmap(word) for word in word_range]
            )

            for word, tick_bitmap in zip(word_range, bitmaps, strict=False):
                if not tick_bitmap:
                    continue

                for b in range(UniswapV3.TICK_WORD_SIZE):
                    if (tick_bitmap >> b) & 1:
                        tick = (word * UniswapV3.TICK_WORD_SIZE + b) * self.tick_spacing
                        ticks.append(tick)

            return ticks

        def liquidity_to_tokens(
            self, liquidity: float, tick_lower: float, tick_upper: float
        ) -> tuple[float, float]:
            sqrtp_lower = math.sqrt(UniswapV3.tick_to_price(tick_lower))
            sqrtp_upper = math.sqrt(UniswapV3.tick_to_price(tick_upper))

            delta_x = (1 / sqrtp_lower - 1 / sqrtp_upper) * liquidity
            delta_y = (sqrtp_upper - sqrtp_lower) * liquidity

            balance_0 = float(delta_x / (10**self.token_0.decimals))
            balance_1 = float(delta_y / (10**self.token_1.decimals))

            return balance_0, balance_1

        async def get_price(self) -> float:
            sqrt96x = (await self._fn_slot0().call())[0]
            return float((sqrt96x**2) / (2**192))

        async def get_normalized_price(self) -> float:
            return float(
                await self.get_price()
                * 10 ** (self.token_0.decimals - self.token_1.decimals)
            )

        async def get_liquidity(self) -> Liquidity | None:
            price = await self.get_price()
            initial_liquidity = await self._fn_liquidity().call()

            calculated_tick = UniswapV3.price_to_tick(price)
            current_tick = int(calculated_tick)
            ticks = await self.get_initialized_ticks(current_tick)

            if not ticks:
                log.warning("No liquidity found")
                return None

            log.debug(f"Found {len(ticks)} initialized ticks!")

            net_liquidity = await self.get_ticks_net_liquidity(ticks)
            ask_path = sorted((t for t in ticks if t <= current_tick), reverse=True)
            bid_path = sorted(t for t in ticks if t > current_tick)
            balance_norm = 10 ** (self.token_1.decimals - self.token_0.decimals)
            primary_0 = self.primary_is_token_0

            def _quote_delta(
                active_L: float, tick_lower: float, tick_upper: float
            ) -> float:
                t0, t1 = self.liquidity_to_tokens(active_L, tick_lower, tick_upper)
                return t1 if primary_0 else t0

            def depth_at(_price: float) -> float:
                if _price <= 0:
                    target: float = (
                        UniswapV3.MIN_TICK if primary_0 else UniswapV3.MAX_TICK
                    )
                elif primary_0:
                    # _price is token_1 per token_0 (e.g. WETH per rETH);
                    # pool tick = log_1.0001(raw_t1/raw_t0) = log(_price * balance_norm)
                    target = UniswapV3.price_to_tick(_price * balance_norm)
                else:
                    target = -UniswapV3.price_to_tick(_price / balance_norm)

                active = initial_liquidity
                last = calculated_tick
                cumulative = 0.0

                if target <= calculated_tick:
                    for tick in ask_path:
                        if target >= tick:
                            return cumulative + _quote_delta(active, target, last)
                        cumulative += _quote_delta(active, tick, last)
                        active -= net_liquidity[tick]
                        last = tick
                else:
                    for tick in bid_path:
                        if target <= tick:
                            return cumulative + _quote_delta(active, last, target)
                        cumulative += _quote_delta(active, last, tick)
                        active += net_liquidity[tick]
                        last = tick

                return cumulative

            quote_price = price / balance_norm if primary_0 else balance_norm / price
            return Liquidity(quote_price, depth_at)

    def __init__(self, pools: list[Pool]):
        super().__init__(pools)

    @classmethod
    async def create(
        cls,
        pool_addresses: list[ChecksumAddress],
        primary_is_token_0: bool = False,
    ) -> "UniswapV3":
        pools = [
            await UniswapV3.Pool.create(addr, primary_is_token_0)
            for addr in pool_addresses
        ]
        return cls(pools)

    def __str__(self) -> str:
        return "Uniswap V3"

    @property
    def color(self) -> str:
        return "#A02C6C"


class UniswapV4(DEX):
    class Pool(UniswapV3.Pool):
        def __init__(
            self,
            pool_id: HexStr,
            state_view: AsyncContract,
            tick_spacing: int,
            token_0: ERC20Token,
            token_1: ERC20Token,
        ):
            self.pool_id = pool_id
            self.state_view = state_view
            self.tick_spacing = tick_spacing
            self.token_0 = token_0
            self.token_1 = token_1
            self.primary_is_token_0 = False

        @classmethod
        async def create(  # type: ignore[override]
            cls,
            pool_id: HexStr,
            tick_spacing: int,
            currency_0: ChecksumAddress,
            currency_1: ChecksumAddress,
        ) -> "UniswapV4.Pool":
            state_view = await rp.get_contract_by_name(
                "UniswapV4StateView", mainnet=True
            )
            token_0 = await ERC20Token.create(currency_0)
            token_1 = await ERC20Token.create(currency_1)
            return cls(pool_id, state_view, tick_spacing, token_0, token_1)

        def _fn_slot0(self) -> AsyncContractFunction:
            return self.state_view.functions.getSlot0(self.pool_id)

        def _fn_liquidity(self) -> AsyncContractFunction:
            return self.state_view.functions.getLiquidity(self.pool_id)

        def _fn_ticks(self, tick: int) -> AsyncContractFunction:
            return self.state_view.functions.getTickInfo(self.pool_id, tick)

        def _fn_tick_bitmap(self, word: int) -> AsyncContractFunction:
            return self.state_view.functions.getTickBitmap(self.pool_id, word)

    def __init__(self, pools: list[Pool]):
        super().__init__(pools)

    @classmethod
    async def create(
        cls,
        pools: list[tuple[HexStr, int, ChecksumAddress, ChecksumAddress]],
    ) -> "UniswapV4":
        return cls([await UniswapV4.Pool.create(*args) for args in pools])

    def __str__(self) -> str:
        return "Uniswap V4"

    @property
    def color(self) -> str:
        return "#D63384"
