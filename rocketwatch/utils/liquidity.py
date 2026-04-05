import logging
import math
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import aiohttp
import numpy as np
from eth_typing import ChecksumAddress, HexStr
from web3.contract import AsyncContract

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
            vault = await rp.get_contract_by_name("BalancerVault", mainnet=True)
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

    def __init__(self, pools: list[WeightedPool]):
        # missing support for other pool types
        super().__init__(pools)

    def __str__(self) -> str:
        return "Balancer"

    @property
    def color(self) -> str:
        return "#C0C0C0"


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
        ):
            self.pool_address = pool_address
            self.contract = contract
            self.tick_spacing = tick_spacing
            self.token_0 = token_0
            self.token_1 = token_1

        @classmethod
        async def create(cls, pool_address: ChecksumAddress) -> "UniswapV3.Pool":
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
            return cls(pool_address, contract, tick_spacing, token_0, token_1)

        def tick_to_word_and_bit(self, tick: int) -> tuple[int, int]:
            compressed = int(tick // self.tick_spacing)
            if (tick < 0) and (tick % self.tick_spacing):
                compressed -= 1

            word_position = int(compressed // UniswapV3.TICK_WORD_SIZE)
            bit_position = compressed % UniswapV3.TICK_WORD_SIZE
            return word_position, bit_position

        async def get_ticks_net_liquidity(self, ticks: list[int]) -> dict[int, int]:
            results = await rp.multicall(
                [self.contract.functions.ticks(tick) for tick in ticks]
            )
            return dict(zip(ticks, [r[1] for r in results], strict=False))

        async def get_initialized_ticks(self, current_tick: int) -> list[int]:
            ticks = []
            active_word, b = self.tick_to_word_and_bit(current_tick)

            word_range = list(range(active_word - 5, active_word + 5))
            bitmaps = await rp.multicall(
                [self.contract.functions.tickBitmap(word) for word in word_range]
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
            sqrt96x = (await self.contract.functions.slot0().call())[0]
            return float((sqrt96x**2) / (2**192))

        async def get_normalized_price(self) -> float:
            return float(
                await self.get_price()
                * 10 ** (self.token_0.decimals - self.token_1.decimals)
            )

        async def get_liquidity(self) -> Liquidity | None:
            price = await self.get_price()
            initial_liquidity = await self.contract.functions.liquidity().call()

            calculated_tick = UniswapV3.price_to_tick(price)
            current_tick = int(calculated_tick)
            ticks = await self.get_initialized_ticks(current_tick)

            if not ticks:
                log.warning("No liquidity found")
                return None

            log.debug(f"Found {len(ticks)} initialized ticks!")

            async def get_cumulative_liquidity(_ticks: list[int]) -> list[float]:
                cumulative_liquidity: float = 0
                last_tick = calculated_tick
                active_liquidity = initial_liquidity

                net_liquidity: dict[int, int] = await self.get_ticks_net_liquidity(
                    _ticks
                )
                liquidity = []

                # assume liquidity in token 0 for now
                for tick in _ticks:
                    if tick > last_tick:
                        liq_0, _ = self.liquidity_to_tokens(
                            active_liquidity, last_tick, tick
                        )
                        active_liquidity += net_liquidity[tick]
                    else:
                        liq_0, _ = self.liquidity_to_tokens(
                            active_liquidity, tick, last_tick
                        )
                        active_liquidity -= net_liquidity[tick]

                    cumulative_liquidity += liq_0
                    liquidity.append(cumulative_liquidity)
                    last_tick = tick

                return liquidity

            _ask_ticks = [t for t in reversed(ticks) if t <= current_tick] + [
                UniswapV3.MIN_TICK
            ]
            ask_liquidity = [0.0] + await get_cumulative_liquidity(_ask_ticks)
            ask_ticks: list[int | float] = [calculated_tick, *_ask_ticks]

            _bid_ticks = [t for t in ticks if t > current_tick] + [UniswapV3.MAX_TICK]
            bid_liquidity = [0.0] + await get_cumulative_liquidity(_bid_ticks)
            bid_ticks: list[int | float] = [calculated_tick, *_bid_ticks]

            balance_norm = 10 ** (self.token_1.decimals - self.token_0.decimals)

            def depth_at(_price: float) -> float:
                tick: float
                if _price <= 0:
                    tick = UniswapV3.MAX_TICK
                else:
                    tick = -UniswapV3.price_to_tick(_price / balance_norm)

                if tick <= calculated_tick:
                    i = int(np.searchsorted(-np.array(ask_ticks), -tick, "right"))
                    liq_ticks = ask_ticks
                    liquidity_levels = ask_liquidity
                else:
                    i = int(np.searchsorted(np.array(bid_ticks), tick, "right"))
                    liq_ticks = bid_ticks
                    liquidity_levels = bid_liquidity

                if i >= len(liquidity_levels):
                    return liquidity_levels[-1]

                range_share = abs(tick - liq_ticks[i - 1]) / abs(
                    liq_ticks[i] - liq_ticks[i - 1]
                )
                range_liquidity = abs(liquidity_levels[i] - liquidity_levels[i - 1])
                # linear interpolation should be fine since ticks are exponential
                return liquidity_levels[i - 1] + range_share * range_liquidity

            return Liquidity(balance_norm / price, depth_at)

    def __init__(self, pools: list[Pool]):
        super().__init__(pools)

    @classmethod
    async def create(cls, pool_addresses: list[ChecksumAddress]) -> "UniswapV3":
        pools = [await UniswapV3.Pool.create(addr) for addr in pool_addresses]
        return cls(pools)

    def __str__(self) -> str:
        return "Uniswap"

    @property
    def color(self) -> str:
        return "#A02C6C"
