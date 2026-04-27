import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, TypedDict, cast

from discord import Interaction
from discord.app_commands import command
from eth_typing import BlockNumber, ChecksumAddress, HexStr
from web3.contract import AsyncContract
from web3.types import EventData

from rocketwatch.bot import RocketWatch
from rocketwatch.utils import solidity
from rocketwatch.utils.embeds import (
    CustomColors,
    Embed,
    build_event_embed,
    build_small_event_embed,
    el_explorer_url,
    format_value,
)
from rocketwatch.utils.event import Event, EventPlugin
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.sea_creatures import get_sea_creature_for_address
from rocketwatch.utils.shared_w3 import w3
from rocketwatch.utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.dex_trades")

_BUY_COLOR = CustomColors.GREEN
_SELL_COLOR = CustomColors.RED

_RPL_USD_THRESHOLD_LARGE = 50_000
_RETH_USD_THRESHOLD_LARGE = 250_000

_RPL_USD_THRESHOLD_SMALL = 5000
_RETH_USD_THRESHOLD_SMALL = 25_000


def _addr(s: str) -> ChecksumAddress:
    return w3.to_checksum_address(s)


_COW_SETTLEMENT = _addr("0x9008D19f58AAbD9eD0D60971565AA8510560ab41")
_UNI_V3_RETH_POOLS = [
    _addr("0x553e9C493678d8606d6a5ba284643dB2110Df823"),
    _addr("0xa4e0faA58465A2D369aa21B3e42d43374c6F9613"),
]
_UNI_V3_RPL_POOLS = [
    _addr("0xe42318eA3b998e8355a3Da364EB9D48eC725Eb45"),
]
_CURVE_POOLS = [
    _addr("0x447Ddd4960d9fdBF6af9a790560d0AF76795CB08"),
    _addr("0xe080027Bd47353b5D1639772b4a75E9Ed3658A0d"),
    _addr("0x9EfE1A1Cbd6Ca51Ee8319AFc4573d253C3B732af"),
]
# V4 uses native ETH (address 0x0) as currency0
_ETH_ADDRESS = _addr("0x0000000000000000000000000000000000000000")
_UNI_V4_RPL_POOL_IDS: list[HexStr] = [
    HexStr("0xf54ebae2cdfe65593f7b9dbf655f498796c7744107a69d78456627faf98dc36f"),
    HexStr("0xd36acc983941d38f6edb0ff6f6ee730e59cba0f2f720fea3ce240ae9b90fc4d0"),
]

_DEX_META: dict[str, tuple[str, str]] = {
    # dex_key -> (emoji, display_name)
    "cow": (":cow:", "CoW"),
    "uniswap": (":unicorn:", "Uniswap"),
    "balancer": ("<:balancer:1494119892369014894>", "Balancer"),
    "curve": ("<:curve:1494119967044665484>", "Curve"),
}

# Swap event topics for the generic decoder.  Every address emitting one of
# these signatures is treated as a pool; the graph builder decodes its log
# regardless of whether it's a pool we actively monitor.
_UNI_V2_SWAP_TOPIC = w3.keccak(
    text="Swap(address,uint256,uint256,uint256,uint256,address)"
)
_UNI_V3_SWAP_TOPIC = w3.keccak(
    text="Swap(address,address,int256,int256,uint160,uint128,int24)"
)
_UNI_V4_SWAP_TOPIC = w3.keccak(
    text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
)
_BAL_V2_SWAP_TOPIC = w3.keccak(text="Swap(bytes32,address,address,uint256,uint256)")
_BAL_V3_SWAP_TOPIC = w3.keccak(
    text="Swap(address,address,address,uint256,uint256,uint256,uint256)"
)
_CURVE_EXCHANGE_TOPIC = w3.keccak(
    text="TokenExchange(address,int128,uint256,int128,uint256)"
)
_WETH_DEPOSIT_TOPIC = w3.keccak(text="Deposit(address,uint256)")
_WETH_WITHDRAWAL_TOPIC = w3.keccak(text="Withdrawal(address,uint256)")
_WETH_ADDRESS = _addr("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")


@dataclass
class DexSwap:
    """Raw normalized swap from any DEX — no RPL/rETH logic here."""

    dex: str  # key into _DEX_META
    sell_token: ChecksumAddress
    sell_amount: int
    buy_token: ChecksumAddress
    buy_amount: int
    owner: ChecksumAddress
    tx_hash: HexStr
    block_number: BlockNumber
    tx_index: int
    log_index: int
    extra_fields: list[tuple[str, str, bool]] = field(default_factory=list)


@dataclass
class _GraphSwap:
    """Single decoded swap leg used to populate the trade graph."""

    pool: ChecksumAddress
    token_in: ChecksumAddress
    amount_in: int
    token_out: ChecksumAddress
    amount_out: int
    owner: ChecksumAddress  # counterparty named in the event (router or user)
    dex: str
    log_index: int


@dataclass
class _TradeGraph:
    # node address -> token address -> signed net flow
    flows: dict[ChecksumAddress, dict[ChecksumAddress, int]]
    pools: set[ChecksumAddress]
    swaps: list[_GraphSwap]


class CoWTradeArgs(TypedDict):
    owner: ChecksumAddress
    sellToken: ChecksumAddress
    buyToken: ChecksumAddress
    sellAmount: int
    buyAmount: int
    feeAmount: int
    orderUid: bytes


class DexTrades(EventPlugin):
    def __init__(self, bot: RocketWatch) -> None:
        super().__init__(bot)
        # contracts (lazily initialized)
        self._settlement: AsyncContract | None = None
        self._balancer_v2_vault: AsyncContract | None = None
        self._balancer_v3_vault: AsyncContract | None = None
        self._curve_pools: list[AsyncContract] | None = None

        self._uni_pools: (
            list[tuple[AsyncContract, ChecksumAddress, ChecksumAddress]] | None
        ) = None
        self._uni_v4_pm: AsyncContract | None = None
        # pool_id -> (token0, token1)
        self._uni_v4_pools: (
            dict[HexStr, tuple[ChecksumAddress, ChecksumAddress]] | None
        ) = None
        # token addresses
        self._rpl: ChecksumAddress | None = None
        self._reth: ChecksumAddress | None = None
        self._tokens: set[ChecksumAddress] | None = None
        self._token_names: dict[ChecksumAddress, str] | None = None
        # Curve pool coin mappings (per pool)
        self._curve_coins: list[dict[int, ChecksumAddress]] | None = None

        # Cache of pool_address -> (token0, token1) for both known and
        # lazily-resolved pools seen during graph building.  Unresolvable
        # addresses are remembered in _unresolvable_pools to avoid retry storms.
        self._pool_tokens: dict[
            ChecksumAddress, tuple[ChecksumAddress, ChecksumAddress]
        ] = {}
        self._unresolvable_pools: set[ChecksumAddress] = set()
        # Generic event decoders (ABI-only; decode logs from any pool address).
        self._v3_swap_decoder: Any = None
        self._v2_swap_decoder: Any = None
        self._curve_swap_decoder: Any = None
        self._bal_v2_swap_decoder: Any = None
        self._bal_v3_swap_decoder: Any = None
        self._v4_swap_decoder: Any = None
        self._weth_deposit_decoder: Any = None
        self._weth_withdrawal_decoder: Any = None

    async def _ensure_setup(self) -> None:
        if self._tokens is not None:
            return

        self._rpl = await rp.get_address_by_name("rocketTokenRPL")
        self._reth = await rp.get_address_by_name("rocketTokenRETH")
        self._tokens = {self._rpl, self._reth}
        self._token_names = {self._rpl: "RPL", self._reth: "rETH"}

        # CoW
        self._settlement = await rp.assemble_contract("CoWSettlement", _COW_SETTLEMENT)

        # Balancer V2 + V3 (distinct vaults, distinct event schemas)
        self._balancer_v2_vault = await rp.get_contract_by_name("BalancerV2Vault")
        self._balancer_v3_vault = await rp.get_contract_by_name("BalancerV3Vault")

        # Curve
        self._curve_pools = []
        self._curve_coins = []
        for pool_addr in _CURVE_POOLS:
            pool = await rp.assemble_contract("curvePool", pool_addr)
            coins: dict[int, ChecksumAddress] = {}
            for i in range(2):
                addr = await pool.functions.coins(i).call()
                coins[i] = w3.to_checksum_address(addr)
            self._curve_pools.append(pool)
            self._curve_coins.append(coins)

        # Uniswap V3 - ABI is "UniswapV3Pool", addresses are per-pool
        self._uni_pools = []
        for pool_addr in _UNI_V3_RETH_POOLS + _UNI_V3_RPL_POOLS:
            pool = await rp.assemble_contract("UniswapV3Pool", pool_addr)
            token0 = w3.to_checksum_address(await pool.functions.token0().call())
            token1 = w3.to_checksum_address(await pool.functions.token1().call())
            self._uni_pools.append((pool, token0, token1))

        # Uniswap V4 - singleton PoolManager, filter by pool ID
        # Pool keys are not stored on-chain in V4, so token pairs are hardcoded.
        self._uni_v4_pm = await rp.get_contract_by_name("UniswapV4PoolManager")
        self._uni_v4_pools = {
            pool_id: (_ETH_ADDRESS, self._rpl) for pool_id in _UNI_V4_RPL_POOL_IDS
        }

        # Pre-populate the pool-token cache for known pools so the graph
        # builder doesn't RPC for them.
        for pool, token0, token1 in self._uni_pools:
            self._pool_tokens[pool.address] = (token0, token1)
        for pool, coins in zip(self._curve_pools, self._curve_coins, strict=True):
            self._pool_tokens[pool.address] = (coins[0], coins[1])

        # Decoders — ABI-bound once, used to decode logs from any address.
        # Uni V3/V2 use an arbitrary address (decoder only reads topics + data).
        v3_reference = self._uni_pools[0][0]
        self._v3_swap_decoder = v3_reference.events.Swap()
        v2_reference = await rp.assemble_contract("UniswapV2Pair", _WETH_ADDRESS)
        self._v2_swap_decoder = v2_reference.events.Swap()
        self._curve_swap_decoder = self._curve_pools[0].events.TokenExchange()
        self._bal_v2_swap_decoder = self._balancer_v2_vault.events.Swap()
        self._bal_v3_swap_decoder = self._balancer_v3_vault.events.Swap()
        self._v4_swap_decoder = self._uni_v4_pm.events.Swap()
        weth = await rp.assemble_contract("WETH", _WETH_ADDRESS)
        self._weth_deposit_decoder = weth.events.Deposit()
        self._weth_withdrawal_decoder = weth.events.Withdrawal()

    # ── Trade graph: decode full receipt into a token-flow graph ────

    async def _get_pool_tokens(
        self, pool_addr: ChecksumAddress
    ) -> tuple[ChecksumAddress, ChecksumAddress] | None:
        """Resolve (token0, token1) for a pool via RPC, caching the result.
        Tries Uni V2/V3 style (token0/token1) first, then Curve (coins).
        Unresolvable addresses are remembered to avoid retry storms.
        """
        if pool_addr in self._pool_tokens:
            return self._pool_tokens[pool_addr]
        if pool_addr in self._unresolvable_pools:
            return None

        try:
            pool = await rp.assemble_contract("UniswapV3Pool", pool_addr)
            t0 = w3.to_checksum_address(await pool.functions.token0().call())
            t1 = w3.to_checksum_address(await pool.functions.token1().call())
        except Exception:
            try:
                pool = await rp.assemble_contract("curvePool", pool_addr)
                t0 = w3.to_checksum_address(await pool.functions.coins(0).call())
                t1 = w3.to_checksum_address(await pool.functions.coins(1).call())
            except Exception:
                self._unresolvable_pools.add(pool_addr)
                return None

        self._pool_tokens[pool_addr] = (t0, t1)
        return (t0, t1)

    async def _decode_swap(
        self, raw_log: Any, tx_from: ChecksumAddress
    ) -> _GraphSwap | None:
        """Decode a single log into a _GraphSwap, dispatching on topic0.
        Returns None if the log is not a swap event we understand or if the
        pool's token pair can't be resolved.  WETH Deposit/Withdrawal are
        modelled as synthetic swaps against a WETH wrapper pool.
        """
        if not raw_log["topics"]:
            return None
        topic0 = raw_log["topics"][0]
        log_index = raw_log["logIndex"]
        pool = w3.to_checksum_address(raw_log["address"])

        try:
            if topic0 == _UNI_V3_SWAP_TOPIC:
                event = self._v3_swap_decoder.process_log(raw_log)
                args = event["args"]
                tokens = await self._get_pool_tokens(pool)
                if tokens is None:
                    return None
                token0, token1 = tokens
                a0, a1 = int(args["amount0"]), int(args["amount1"])
                # V3: positive = pool received (user sold), negative = pool sent
                if a0 > 0:
                    token_in, amt_in, token_out, amt_out = token0, a0, token1, -a1
                else:
                    token_in, amt_in, token_out, amt_out = token1, a1, token0, -a0
                owner = w3.to_checksum_address(args["recipient"])
                return _GraphSwap(
                    pool,
                    token_in,
                    amt_in,
                    token_out,
                    amt_out,
                    owner,
                    "uniswap",
                    log_index,
                )

            if topic0 == _UNI_V2_SWAP_TOPIC:
                event = self._v2_swap_decoder.process_log(raw_log)
                args = event["args"]
                tokens = await self._get_pool_tokens(pool)
                if tokens is None:
                    return None
                token0, token1 = tokens
                a0_in = int(args["amount0In"])
                a1_in = int(args["amount1In"])
                a0_out = int(args["amount0Out"])
                a1_out = int(args["amount1Out"])
                if a0_in > 0:
                    token_in, amt_in, token_out, amt_out = token0, a0_in, token1, a1_out
                else:
                    token_in, amt_in, token_out, amt_out = token1, a1_in, token0, a0_out
                owner = w3.to_checksum_address(args["to"])
                return _GraphSwap(
                    pool,
                    token_in,
                    amt_in,
                    token_out,
                    amt_out,
                    owner,
                    "uniswap",
                    log_index,
                )

            if topic0 == _UNI_V4_SWAP_TOPIC:
                event = self._v4_swap_decoder.process_log(raw_log)
                args = event["args"]
                pool_id = HexStr("0x" + args["id"].hex())
                # V4 pool keys aren't stored on-chain; only decode pools we
                # know the token pair for.
                assert self._uni_v4_pools is not None
                if pool_id not in self._uni_v4_pools:
                    return None
                token0, token1 = self._uni_v4_pools[pool_id]
                a0, a1 = int(args["amount0"]), int(args["amount1"])
                # V4: from user's perspective — positive = user received
                if a0 > 0:
                    token_in, amt_in, token_out, amt_out = token1, -a1, token0, a0
                else:
                    token_in, amt_in, token_out, amt_out = token0, -a0, token1, a1
                # V4 Swap event has no user field; fall back to tx sender
                return _GraphSwap(
                    pool,
                    token_in,
                    amt_in,
                    token_out,
                    amt_out,
                    tx_from,
                    "uniswap",
                    log_index,
                )

            if topic0 == _BAL_V2_SWAP_TOPIC:
                event = self._bal_v2_swap_decoder.process_log(raw_log)
                args = event["args"]
                token_in = w3.to_checksum_address(args["tokenIn"])
                token_out = w3.to_checksum_address(args["tokenOut"])
                return _GraphSwap(
                    pool,
                    token_in,
                    int(args["amountIn"]),
                    token_out,
                    int(args["amountOut"]),
                    tx_from,
                    "balancer",
                    log_index,
                )

            if topic0 == _BAL_V3_SWAP_TOPIC:
                event = self._bal_v3_swap_decoder.process_log(raw_log)
                args = event["args"]
                # Balancer V3 Swap's topic[1] is the pool address, not the
                # vault — treat that as the pool node so multiple V3 pools
                # under the same vault don't collapse.
                pool = w3.to_checksum_address(args["pool"])
                token_in = w3.to_checksum_address(args["tokenIn"])
                token_out = w3.to_checksum_address(args["tokenOut"])
                return _GraphSwap(
                    pool,
                    token_in,
                    int(args["amountIn"]),
                    token_out,
                    int(args["amountOut"]),
                    tx_from,
                    "balancer",
                    log_index,
                )

            if topic0 == _CURVE_EXCHANGE_TOPIC:
                event = self._curve_swap_decoder.process_log(raw_log)
                args = event["args"]
                tokens = await self._get_pool_tokens(pool)
                if tokens is None:
                    return None
                coins = {0: tokens[0], 1: tokens[1]}
                sold_id = int(args["sold_id"])
                bought_id = int(args["bought_id"])
                if sold_id not in coins or bought_id not in coins:
                    return None  # pool with >2 coins; fall back to skip
                return _GraphSwap(
                    pool,
                    coins[sold_id],
                    int(args["tokens_sold"]),
                    coins[bought_id],
                    int(args["tokens_bought"]),
                    w3.to_checksum_address(args["buyer"]),
                    "curve",
                    log_index,
                )

            if topic0 == _WETH_DEPOSIT_TOPIC and pool == _WETH_ADDRESS:
                event = self._weth_deposit_decoder.process_log(raw_log)
                args = event["args"]
                amount = int(args["wad"])
                dst = w3.to_checksum_address(args["dst"])
                # Model wrap as ETH→WETH swap at 1:1 through the WETH contract
                return _GraphSwap(
                    _WETH_ADDRESS,
                    _ETH_ADDRESS,
                    amount,
                    _WETH_ADDRESS,
                    amount,
                    dst,
                    "uniswap",
                    log_index,
                )

            if topic0 == _WETH_WITHDRAWAL_TOPIC and pool == _WETH_ADDRESS:
                event = self._weth_withdrawal_decoder.process_log(raw_log)
                args = event["args"]
                amount = int(args["wad"])
                src = w3.to_checksum_address(args["src"])
                return _GraphSwap(
                    _WETH_ADDRESS,
                    _WETH_ADDRESS,
                    amount,
                    _ETH_ADDRESS,
                    amount,
                    src,
                    "uniswap",
                    log_index,
                )
        except Exception:
            log.debug("Failed to decode swap log %s", log_index, exc_info=True)
            return None

        return None

    async def _build_trade_graph(
        self, tx_hash: HexStr, tx_from: ChecksumAddress, receipt_logs: list[Any]
    ) -> _TradeGraph:
        """Decode every swap-like log in the receipt and build a flow graph:
        each swap contributes +in/-out at the pool node and -in/+out at the
        counterparty.  Multi-hop trades through unmonitored pools therefore
        show up as complete end-to-end flows at the initiating trader node.
        """
        flows: dict[ChecksumAddress, dict[ChecksumAddress, int]] = {}
        pools: set[ChecksumAddress] = set()
        swaps: list[_GraphSwap] = []

        for raw_log in receipt_logs:
            decoded = await self._decode_swap(raw_log, tx_from)
            if decoded is None:
                continue
            swaps.append(decoded)
            pools.add(decoded.pool)

            pool_flows = flows.setdefault(decoded.pool, {})
            pool_flows[decoded.token_in] = (
                pool_flows.get(decoded.token_in, 0) + decoded.amount_in
            )
            pool_flows[decoded.token_out] = (
                pool_flows.get(decoded.token_out, 0) - decoded.amount_out
            )

            owner_flows = flows.setdefault(decoded.owner, {})
            owner_flows[decoded.token_in] = (
                owner_flows.get(decoded.token_in, 0) - decoded.amount_in
            )
            owner_flows[decoded.token_out] = (
                owner_flows.get(decoded.token_out, 0) + decoded.amount_out
            )

        return _TradeGraph(flows=flows, pools=pools, swaps=swaps)

    def _resolve_graph(
        self,
        graph: _TradeGraph,
        tx_hash: HexStr,
        block_number: BlockNumber,
        tx_index: int,
        extra_fields: list[tuple[str, str, bool]],
    ) -> list[DexSwap]:
        """Emit one DexSwap per non-pool node whose net flow includes a
        tracked token (RPL/rETH).  Counter-side is picked from the full flow
        map so multi-hop routes through untracked tokens resolve correctly.
        """
        assert self._tokens is not None

        result: list[DexSwap] = []
        for node, flows in graph.flows.items():
            if node in graph.pools or node == _ETH_ADDRESS:
                continue

            tracked_flows = {
                t: v for t, v in flows.items() if t in self._tokens and v != 0
            }
            if not tracked_flows:
                continue

            tracked = max(tracked_flows, key=lambda t: abs(tracked_flows[t]))
            tracked_amount = tracked_flows[tracked]

            if tracked_amount > 0:
                buy_token = tracked
                sell_token = min(flows, key=lambda t: flows[t])
                if flows[sell_token] >= 0 or sell_token == buy_token:
                    continue
                buy_amount = tracked_amount
                sell_amount = abs(flows[sell_token])
            else:
                sell_token = tracked
                buy_token = max(flows, key=lambda t: flows[t])
                if flows[buy_token] <= 0 or buy_token == sell_token:
                    continue
                sell_amount = abs(tracked_amount)
                buy_amount = flows[buy_token]

            # Attribute dex by whichever swap touched this trader with the
            # largest tracked-token amount.
            dex = "uniswap"
            best_touch = 0
            best_log_index = 0
            for s in graph.swaps:
                if s.owner != node:
                    continue
                touched = 0
                if s.token_in == tracked:
                    touched = s.amount_in
                elif s.token_out == tracked:
                    touched = s.amount_out
                if touched > best_touch:
                    best_touch = touched
                    dex = s.dex
                    best_log_index = s.log_index

            result.append(
                DexSwap(
                    dex=dex,
                    sell_token=sell_token,
                    sell_amount=sell_amount,
                    buy_token=buy_token,
                    buy_amount=buy_amount,
                    owner=node,
                    tx_hash=tx_hash,
                    block_number=block_number,
                    tx_index=tx_index,
                    log_index=best_log_index,
                    extra_fields=list(extra_fields),
                )
            )

        return result

    async def _aggregate_by_tx(self, all_swaps: list[DexSwap]) -> list[DexSwap]:
        """For each tx with at least one monitored-pool swap, fetch the full
        receipt and build a trade graph spanning every swap event (including
        unmonitored pools).  Emit one DexSwap per non-pool node with a
        non-zero tracked-token flow.  CoW trades are authoritative and
        short-circuit the graph; their underlying DEX legs are discarded.
        """
        by_tx: dict[HexStr, list[DexSwap]] = {}
        for swap in all_swaps:
            by_tx.setdefault(swap.tx_hash, []).append(swap)

        result: list[DexSwap] = []
        for tx_hash, swaps in by_tx.items():
            cow_swaps = [s for s in swaps if s.dex == "cow"]
            if cow_swaps:
                result.extend(cow_swaps)
                continue

            try:
                tx = await w3.eth.get_transaction(tx_hash)
                receipt = await w3.eth.get_transaction_receipt(tx_hash)
                tx_from = w3.to_checksum_address(tx["from"])
                graph = await self._build_trade_graph(
                    tx_hash, tx_from, list(receipt["logs"])
                )
            except Exception:
                log.exception("Failed to build trade graph for %s", tx_hash)
                continue

            extra_fields = [f for s in swaps for f in s.extra_fields]
            ref = swaps[0]
            resolved = self._resolve_graph(
                graph,
                tx_hash,
                ref.block_number,
                ref.tx_index,
                extra_fields,
            )
            if not resolved:
                log.debug("Trade graph for %s resolved to no traders", tx_hash)
            result.extend(resolved)

        return result

    async def _get_prices(self) -> tuple[float, float]:
        """Return (rpl_usd, reth_usd) prices."""
        rpl_ratio = solidity.to_float(await rp.call("rocketNetworkPrices.getRPLPrice"))
        reth_ratio = solidity.to_float(await rp.call("rocketTokenRETH.getExchangeRate"))
        eth_usd = await rp.get_eth_usdc_price()
        return rpl_ratio * eth_usd, reth_ratio * eth_usd

    async def _resolve_token(self, address: ChecksumAddress) -> tuple[str, int]:
        """Return (symbol, decimals) for an ERC-20 address."""
        # Native ETH is represented by 0x0 (Uni V4) or 0xEE..EE (some routers)
        if address == _ETH_ADDRESS or address == w3.to_checksum_address(
            "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        ):
            return "ETH", 18
        decimals = 18
        erc20 = await rp.assemble_contract(name="ERC20", address=address)
        with contextlib.suppress(Exception):
            decimals = await erc20.functions.decimals().call()
        try:
            symbol = await erc20.functions.symbol().call()
        except Exception:
            symbol = "UNKWN"
        return symbol, decimals

    async def _process_swaps(
        self, raw_swaps: list[DexSwap], rpl_usd: float, reth_usd: float
    ) -> list[Event]:
        """Filter for RPL/rETH relevance, apply thresholds, build events."""
        assert self._tokens is not None
        assert self._token_names is not None

        events: list[Event] = []
        for swap in raw_swaps:
            # When both sides are tracked (RPL↔rETH), classify by RPL:
            # RPL moves are more notable to this audience than rETH moves.
            if self._rpl in (swap.buy_token, swap.sell_token):
                tracked = self._rpl
            elif self._reth in (swap.buy_token, swap.sell_token):
                tracked = self._reth
            else:
                continue

            is_buy = swap.buy_token == tracked
            token = self._token_names[tracked]
            if is_buy:
                our_amount = swap.buy_amount
                other_address = swap.sell_token
                other_amount = swap.sell_amount
            else:
                our_amount = swap.sell_amount
                other_address = swap.buy_token
                other_amount = swap.buy_amount

            our_amount_f = solidity.to_float(our_amount, 18)

            # USD value and threshold check
            price = rpl_usd if token == "RPL" else reth_usd
            upper_threshold = (
                _RPL_USD_THRESHOLD_LARGE
                if token == "RPL"
                else _RETH_USD_THRESHOLD_LARGE
            )
            lower_threshold = (
                _RPL_USD_THRESHOLD_SMALL
                if token == "RPL"
                else _RETH_USD_THRESHOLD_SMALL
            )

            usd_value = our_amount_f * price

            if usd_value < lower_threshold:
                continue

            other_symbol, other_decimals = await self._resolve_token(other_address)
            other_amount_f = solidity.to_float(other_amount, other_decimals)

            emoji, _dex_name = _DEX_META[swap.dex]
            action = "Buy" if is_buy else "Sell"
            event_name = f"{swap.dex}_{'buy' if is_buy else 'sell'}_{token.lower()}"
            verb = "bought" if is_buy else "sold"

            color = _BUY_COLOR if is_buy else _SELL_COLOR
            sea = await get_sea_creature_for_address(swap.owner)
            owner_link = await el_explorer_url(swap.owner)

            if usd_value < upper_threshold:
                description = f"{emoji} {sea}{owner_link} {verb} **{format_value(our_amount_f)} {token}**!"
                embed = await build_small_event_embed(
                    description=description,
                    tx_hash=swap.tx_hash,
                )
                embed.color = color
            else:
                title = f"{emoji} {token} {action}"
                description = (
                    f"{sea}{owner_link} {verb} **{format_value(our_amount_f)} {token}**"
                    f" for {format_value(other_amount_f)} {other_symbol}!"
                )

                embed = await build_event_embed(
                    tx_hash=swap.tx_hash,
                    block_number=swap.block_number,
                    color=color,
                    title=title,
                    description=description,
                    fields=swap.extra_fields or None,
                )

            events.append(
                Event(
                    embed=embed,
                    topic="dex_trade",
                    block_number=swap.block_number,
                    event_name=event_name,
                    unique_id=f"dex_trade_{swap.tx_hash}:{swap.owner}",
                    transaction_index=swap.tx_index,
                    event_index=swap.log_index,
                )
            )

        return events

    # ── CoW Protocol ────────────────────────────────────────────────

    async def _fetch_cow_swaps(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[DexSwap]:
        assert self._settlement is not None

        trade_event = self._settlement.events.Trade()
        logs = await w3.eth.get_logs(
            {
                "address": self._settlement.address,
                "topics": [trade_event.topic],
                "fromBlock": from_block,
                "toBlock": to_block,
            }
        )
        if not logs:
            return []

        trades: list[EventData] = [trade_event.process_log(log) for log in logs]

        swaps: list[DexSwap] = []
        for trade in trades:
            args = cast(CoWTradeArgs, trade["args"])
            cow_uid = f"0x{args['orderUid'].hex()}"
            cow_link = f"[ORDER](https://explorer.cow.fi/orders/{cow_uid})"

            swaps.append(
                DexSwap(
                    dex="cow",
                    sell_token=w3.to_checksum_address(args["sellToken"]),
                    sell_amount=args["sellAmount"],
                    buy_token=w3.to_checksum_address(args["buyToken"]),
                    buy_amount=args["buyAmount"],
                    owner=w3.to_checksum_address(args["owner"]),
                    tx_hash=HexStr(trade["transactionHash"].to_0x_hex()),
                    block_number=BlockNumber(trade["blockNumber"]),
                    tx_index=trade["transactionIndex"],
                    log_index=trade["logIndex"],
                    extra_fields=[("CoW Order", cow_link, False)],
                )
            )

        return swaps

    # ── Uniswap V3 ─────────────────────────────────────────────────

    async def _fetch_uniswap_swaps(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[DexSwap]:
        assert self._uni_pools is not None
        swaps: list[DexSwap] = []

        for pool, token0, token1 in self._uni_pools:
            swap_event = pool.events.Swap()
            logs = await w3.eth.get_logs(
                {
                    "address": pool.address,
                    "topics": [swap_event.topic],
                    "fromBlock": from_block,
                    "toBlock": to_block,
                }
            )
            if not logs:
                continue

            decoded: list[EventData] = [swap_event.process_log(log) for log in logs]
            for event in decoded:
                args = event["args"]
                amount0: int = args["amount0"]
                amount1: int = args["amount1"]

                # Positive = tokens flowing into the pool (user sells that token)
                # Negative = tokens flowing out (user buys that token)
                if amount0 > 0:
                    sell_token, sell_amount = token0, amount0
                    buy_token, buy_amount = token1, abs(amount1)
                else:
                    sell_token, sell_amount = token1, abs(amount1)
                    buy_token, buy_amount = token0, abs(amount0)

                swaps.append(
                    DexSwap(
                        dex="uniswap",
                        sell_token=sell_token,
                        sell_amount=sell_amount,
                        buy_token=buy_token,
                        buy_amount=buy_amount,
                        owner=w3.to_checksum_address(args["recipient"]),
                        tx_hash=HexStr(event["transactionHash"].to_0x_hex()),
                        block_number=BlockNumber(event["blockNumber"]),
                        tx_index=event["transactionIndex"],
                        log_index=event["logIndex"],
                    )
                )

        return swaps

    # ── Balancer ────────────────────────────────────────────────────

    async def _fetch_balancer_v2_swaps(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[DexSwap]:
        assert self._balancer_v2_vault is not None
        assert self._tokens is not None

        swap_event = self._balancer_v2_vault.events.Swap()
        # tokenIn and tokenOut are indexed (topics[2] and topics[3])
        # Query once for tokenIn=RPL/rETH, once for tokenOut=RPL/rETH
        padded_tokens = ["0x" + addr[2:].lower().zfill(64) for addr in self._tokens]

        all_logs = []
        for topic_pos in (2, 3):  # tokenIn, tokenOut
            topics: list[Any] = [swap_event.topic, None, None, None]
            topics[topic_pos] = padded_tokens
            all_logs.extend(
                await w3.eth.get_logs(
                    {
                        "address": self._balancer_v2_vault.address,
                        "topics": topics,
                        "fromBlock": from_block,
                        "toBlock": to_block,
                    }
                )
            )

        if not all_logs:
            return []

        # Deduplicate (a swap with RPL->rETH would appear in both queries)
        seen: set[tuple[Any, ...]] = set()
        unique_logs = []
        for raw_log in all_logs:
            key = (raw_log["transactionHash"], raw_log["logIndex"])
            if key not in seen:
                seen.add(key)
                unique_logs.append(raw_log)

        decoded: list[EventData] = [swap_event.process_log(log) for log in unique_logs]

        swaps: list[DexSwap] = []
        for event in decoded:
            args = event["args"]
            token_in = w3.to_checksum_address(args["tokenIn"])
            token_out = w3.to_checksum_address(args["tokenOut"])

            # Fetch tx sender since Balancer Swap event has no user field
            tx_hash = HexStr(event["transactionHash"].to_0x_hex())
            tx = await w3.eth.get_transaction(tx_hash)
            owner = w3.to_checksum_address(tx["from"])

            swaps.append(
                DexSwap(
                    dex="balancer",
                    sell_token=token_in,
                    sell_amount=args["amountIn"],
                    buy_token=token_out,
                    buy_amount=args["amountOut"],
                    owner=owner,
                    tx_hash=tx_hash,
                    block_number=BlockNumber(event["blockNumber"]),
                    tx_index=event["transactionIndex"],
                    log_index=event["logIndex"],
                )
            )

        return swaps

    # ── Balancer V3 ─────────────────────────────────────────────────

    async def _fetch_balancer_v3_swaps(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[DexSwap]:
        assert self._balancer_v3_vault is not None
        assert self._tokens is not None

        swap_event = self._balancer_v3_vault.events.Swap()
        # V3 Swap: (pool, tokenIn, tokenOut, ...) — topic[0]=sig, [1]=pool,
        # [2]=tokenIn, [3]=tokenOut. Filter token topics same way as V2.
        padded_tokens = ["0x" + addr[2:].lower().zfill(64) for addr in self._tokens]

        all_logs = []
        for topic_pos in (2, 3):
            topics: list[Any] = [swap_event.topic, None, None, None]
            topics[topic_pos] = padded_tokens
            all_logs.extend(
                await w3.eth.get_logs(
                    {
                        "address": self._balancer_v3_vault.address,
                        "topics": topics,
                        "fromBlock": from_block,
                        "toBlock": to_block,
                    }
                )
            )

        if not all_logs:
            return []

        seen: set[tuple[Any, ...]] = set()
        unique_logs = []
        for raw_log in all_logs:
            key = (raw_log["transactionHash"], raw_log["logIndex"])
            if key not in seen:
                seen.add(key)
                unique_logs.append(raw_log)

        decoded: list[EventData] = [swap_event.process_log(log) for log in unique_logs]

        swaps: list[DexSwap] = []
        for event in decoded:
            args = event["args"]
            token_in = w3.to_checksum_address(args["tokenIn"])
            token_out = w3.to_checksum_address(args["tokenOut"])

            tx_hash = HexStr(event["transactionHash"].to_0x_hex())
            tx = await w3.eth.get_transaction(tx_hash)
            owner = w3.to_checksum_address(tx["from"])

            swaps.append(
                DexSwap(
                    dex="balancer",
                    sell_token=token_in,
                    sell_amount=args["amountIn"],
                    buy_token=token_out,
                    buy_amount=args["amountOut"],
                    owner=owner,
                    tx_hash=tx_hash,
                    block_number=BlockNumber(event["blockNumber"]),
                    tx_index=event["transactionIndex"],
                    log_index=event["logIndex"],
                )
            )

        return swaps

    # ── Curve ───────────────────────────────────────────────────────

    async def _fetch_curve_swaps(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[DexSwap]:
        assert self._curve_pools is not None
        assert self._curve_coins is not None

        swaps: list[DexSwap] = []
        for pool, coins in zip(self._curve_pools, self._curve_coins, strict=True):
            exchange_event = pool.events.TokenExchange()
            logs = await w3.eth.get_logs(
                {
                    "address": pool.address,
                    "topics": [exchange_event.topic],
                    "fromBlock": from_block,
                    "toBlock": to_block,
                }
            )
            if not logs:
                continue

            decoded: list[EventData] = [exchange_event.process_log(raw) for raw in logs]

            for event in decoded:
                args = event["args"]
                swaps.append(
                    DexSwap(
                        dex="curve",
                        sell_token=coins[args["sold_id"]],
                        sell_amount=args["tokens_sold"],
                        buy_token=coins[args["bought_id"]],
                        buy_amount=args["tokens_bought"],
                        owner=w3.to_checksum_address(args["buyer"]),
                        tx_hash=HexStr(event["transactionHash"].to_0x_hex()),
                        block_number=BlockNumber(event["blockNumber"]),
                        tx_index=event["transactionIndex"],
                        log_index=event["logIndex"],
                    )
                )

        return swaps

    # ── Uniswap V4 ─────────────────────────────────────────────────

    async def _fetch_uniswap_v4_swaps(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[DexSwap]:
        assert self._uni_v4_pm is not None
        assert self._uni_v4_pools is not None

        swap_event = self._uni_v4_pm.events.Swap()
        pool_id_topics = list(self._uni_v4_pools.keys())

        logs = await w3.eth.get_logs(
            {
                "address": self._uni_v4_pm.address,
                "topics": [swap_event.topic, pool_id_topics],
                "fromBlock": from_block,
                "toBlock": to_block,
            }
        )
        if not logs:
            return []

        decoded: list[EventData] = [swap_event.process_log(raw) for raw in logs]

        swaps: list[DexSwap] = []
        for event in decoded:
            args = event["args"]
            pool_id = HexStr("0x" + args["id"].hex())
            token0, token1 = self._uni_v4_pools[pool_id]

            amount0: int = args["amount0"]
            amount1: int = args["amount1"]

            # V4 convention is opposite to V3: amounts are from the user's
            # perspective. Positive = user received, negative = user paid.
            if amount0 > 0:
                buy_token, buy_amount = token0, amount0
                sell_token, sell_amount = token1, abs(amount1)
            else:
                buy_token, buy_amount = token1, abs(amount1)
                sell_token, sell_amount = token0, abs(amount0)

            tx_hash = HexStr(event["transactionHash"].to_0x_hex())
            tx = await w3.eth.get_transaction(tx_hash)
            owner = w3.to_checksum_address(tx["from"])

            swaps.append(
                DexSwap(
                    dex="uniswap",
                    sell_token=sell_token,
                    sell_amount=sell_amount,
                    buy_token=buy_token,
                    buy_amount=buy_amount,
                    owner=owner,
                    tx_hash=tx_hash,
                    block_number=BlockNumber(event["blockNumber"]),
                    tx_index=event["transactionIndex"],
                    log_index=event["logIndex"],
                )
            )

        return swaps

    # ── Event collection ────────────────────────────────────────────

    async def _get_new_events(self) -> list[Event]:
        from_block = self.last_served_block + 1 - self.lookback_distance
        return await self.get_past_events(BlockNumber(from_block), self._pending_block)

    async def get_past_events(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[Event]:
        await self._ensure_setup()

        rpl_usd, reth_usd = await self._get_prices()

        all_swaps: list[DexSwap] = []
        for fetcher in (
            self._fetch_cow_swaps,
            self._fetch_uniswap_swaps,
            self._fetch_uniswap_v4_swaps,
            self._fetch_balancer_v2_swaps,
            self._fetch_balancer_v3_swaps,
            self._fetch_curve_swaps,
        ):
            try:
                all_swaps.extend(await fetcher(from_block, to_block))
            except Exception:
                log.exception("Failed to fetch swaps from %s", fetcher.__name__)

        all_swaps = await self._aggregate_by_tx(all_swaps)

        return await self._process_swaps(all_swaps, rpl_usd, reth_usd)

    # ── Commands ────────────────────────────────────────────────────

    @command()
    async def cow(self, interaction: Interaction, etherscan_url: str) -> None:
        if "etherscan.io/tx/" not in etherscan_url:
            await interaction.response.send_message(
                "Invalid Etherscan URL", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=is_hidden(interaction))
        url = etherscan_url.replace("etherscan.io", "explorer.cow.fi")
        embed = Embed(description=f"[CoW Explorer]({url})")
        await interaction.followup.send(embed=embed)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(DexTrades(bot))
