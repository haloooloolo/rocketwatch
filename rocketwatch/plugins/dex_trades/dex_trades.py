import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, TypedDict, cast

from discord import Color, Interaction
from discord.app_commands import command
from eth_typing import BlockNumber, ChecksumAddress, HexStr
from web3.contract import AsyncContract
from web3.types import EventData

from rocketwatch.bot import RocketWatch
from rocketwatch.utils import solidity
from rocketwatch.utils.embeds import (
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

_BUY_COLOR = Color.from_rgb(86, 235, 86)
_SELL_COLOR = Color.from_rgb(235, 86, 86)

_RPL_USD_THRESHOLD_LARGE = 10_000
_RETH_USD_THRESHOLD_LARGE = 100_000

_RPL_USD_THRESHOLD_SMALL = 1_000
_RETH_USD_THRESHOLD_SMALL = 10_000


def _addr(s: str) -> ChecksumAddress:
    return w3.to_checksum_address(s)


_COW_SETTLEMENT = _addr("0x9008D19f58AAbD9eD0D60971565AA8510560ab41")
_UNI_RETH_POOLS = [
    _addr("0x553e9C493678d8606d6a5ba284643dB2110Df823"),
    _addr("0xa4e0faA58465A2D369aa21B3e42d43374c6F9613"),
]
_UNI_RPL_POOLS = [
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
        self._balancer_vault: AsyncContract | None = None
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

    async def _ensure_setup(self) -> None:
        if self._tokens is not None:
            return

        self._rpl = await rp.get_address_by_name("rocketTokenRPL")
        self._reth = await rp.get_address_by_name("rocketTokenRETH")
        self._tokens = {self._rpl, self._reth}
        self._token_names = {self._rpl: "RPL", self._reth: "rETH"}

        # CoW
        self._settlement = await rp.assemble_contract("CoWSettlement", _COW_SETTLEMENT)

        # Balancer
        self._balancer_vault = await rp.get_contract_by_name("BalancerVault")

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
        for pool_addr in _UNI_RETH_POOLS + _UNI_RPL_POOLS:
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

    # ── Shared: filtering, classification, event building ───────────

    _TRANSFER_TOPIC = w3.keccak(text="Transfer(address,address,uint256)")
    _WETH_DEPOSIT_TOPIC = w3.keccak(text="Deposit(address,uint256)")
    _WETH_WITHDRAWAL_TOPIC = w3.keccak(text="Withdrawal(address,uint256)")
    _WETH = w3.to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")

    async def _aggregate_by_tx(self, all_swaps: list[DexSwap]) -> list[DexSwap]:
        """Aggregate swaps within the same transaction.

        CoW Trade events already represent the full user trade, so if a tx
        contains CoW swaps we keep only those (underlying DEX events would
        double-count).  For non-CoW transactions with multiple swaps, fetch the
        full receipt and net ERC-20 Transfer events for the tx sender to capture
        the true end-to-end flow (including hops through pools we don't monitor).
        """
        by_tx: dict[HexStr, list[DexSwap]] = {}
        for swap in all_swaps:
            by_tx.setdefault(swap.tx_hash, []).append(swap)

        result: list[DexSwap] = []
        for tx_hash, swaps in by_tx.items():
            cow_swaps = [s for s in swaps if s.dex == "cow"]
            if cow_swaps:
                # CoW events are authoritative; drop underlying DEX legs
                result.extend(cow_swaps)
                continue

            if len(swaps) == 1:
                result.append(swaps[0])
                continue

            aggregated = await self._aggregate_from_receipt(tx_hash, swaps)
            if aggregated:
                result.append(aggregated)
            else:
                # Fallback: keep the largest individual swap
                result.append(max(swaps, key=lambda s: s.buy_amount + s.sell_amount))

        return result

    async def _aggregate_from_receipt(
        self, tx_hash: HexStr, swaps: list[DexSwap]
    ) -> DexSwap | None:
        """Net ERC-20 Transfer events for the tx sender to get the true
        end-to-end swap, even through intermediate pools we don't monitor.

        Also accounts for native ETH via WETH Deposit/Withdrawal events:
        a Deposit means ETH was wrapped (spent), a Withdrawal means ETH
        was unwrapped (received).  Both are credited as WETH flows for
        the user so that ETH<->WETH conversions don't create gaps.
        """
        tx = await w3.eth.get_transaction(tx_hash)
        user = w3.to_checksum_address(tx["from"])
        receipt = await w3.eth.get_transaction_receipt(tx_hash)

        flows: dict[ChecksumAddress, int] = {}
        for log in receipt["logs"]:
            if not log["topics"]:
                continue
            topic0 = log["topics"][0]

            # ERC-20 Transfer
            if topic0 == self._TRANSFER_TOPIC and len(log["topics"]) >= 3:
                from_addr = w3.to_checksum_address(log["topics"][1][-20:])
                to_addr = w3.to_checksum_address(log["topics"][2][-20:])
                amount = int.from_bytes(bytes(log["data"]), "big")
                token = w3.to_checksum_address(log["address"])

                if to_addr == user:
                    flows[token] = flows.get(token, 0) + amount
                if from_addr == user:
                    flows[token] = flows.get(token, 0) - amount

            # WETH Withdrawal → user received native ETH (treat as +WETH)
            elif (
                topic0 == self._WETH_WITHDRAWAL_TOPIC
                and w3.to_checksum_address(log["address"]) == self._WETH
            ):
                src = w3.to_checksum_address(log["topics"][1][-20:])
                amount = int.from_bytes(bytes(log["data"]), "big")
                if src == user:
                    flows[self._WETH] = flows.get(self._WETH, 0) + amount

            # WETH Deposit → user spent native ETH (treat as -WETH)
            elif (
                topic0 == self._WETH_DEPOSIT_TOPIC
                and w3.to_checksum_address(log["address"]) == self._WETH
            ):
                dst = w3.to_checksum_address(log["topics"][1][-20:])
                amount = int.from_bytes(bytes(log["data"]), "big")
                if dst == user:
                    flows[self._WETH] = flows.get(self._WETH, 0) - amount

        if not flows:
            return None

        buy_token = max(flows, key=lambda t: flows[t])
        sell_token = min(flows, key=lambda t: flows[t])

        if flows[buy_token] <= 0 or flows[sell_token] >= 0:
            return None

        ref = max(swaps, key=lambda s: s.buy_amount + s.sell_amount)

        return DexSwap(
            dex=ref.dex,
            sell_token=sell_token,
            sell_amount=abs(flows[sell_token]),
            buy_token=buy_token,
            buy_amount=flows[buy_token],
            owner=user,
            tx_hash=tx_hash,
            block_number=ref.block_number,
            tx_index=ref.tx_index,
            log_index=ref.log_index,
            extra_fields=[f for s in swaps for f in s.extra_fields],
        )

    async def _get_prices(self) -> tuple[float, float]:
        """Return (rpl_usd, reth_usd) prices."""
        rpl_ratio = solidity.to_float(await rp.call("rocketNetworkPrices.getRPLPrice"))
        reth_ratio = solidity.to_float(await rp.call("rocketTokenRETH.getExchangeRate"))
        eth_usd = await rp.get_eth_usdc_price()
        return rpl_ratio * eth_usd, reth_ratio * eth_usd

    async def _resolve_token(self, address: ChecksumAddress) -> tuple[str, int]:
        """Return (symbol, decimals) for an ERC-20 address."""
        decimals = 18
        erc20 = await rp.assemble_contract(name="ERC20", address=address)
        with contextlib.suppress(Exception):
            decimals = await erc20.functions.decimals().call()
        try:
            symbol = await erc20.functions.symbol().call()
        except Exception:
            symbol = (
                "ETH"
                if address
                == w3.to_checksum_address("0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")
                else "UNKWN"
            )
        return symbol, decimals

    async def _process_swaps(
        self, raw_swaps: list[DexSwap], rpl_usd: float, reth_usd: float
    ) -> list[Event]:
        """Filter for RPL/rETH relevance, apply thresholds, build events."""
        assert self._tokens is not None
        assert self._token_names is not None

        events: list[Event] = []
        for swap in raw_swaps:
            # Determine if this swap involves a tracked token and which side
            if swap.buy_token in self._tokens:
                token = self._token_names[swap.buy_token]
                is_buy = True
                our_amount = swap.buy_amount
                other_address = swap.sell_token
                other_amount = swap.sell_amount
            elif swap.sell_token in self._tokens:
                token = self._token_names[swap.sell_token]
                is_buy = False
                our_amount = swap.sell_amount
                other_address = swap.buy_token
                other_amount = swap.buy_amount
            else:
                continue

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
            owner_link = await el_explorer_url(swap.owner)

            if usd_value < upper_threshold:
                description = f"{emoji} {owner_link} {verb} **{format_value(our_amount_f)} {token}**"
                embed = await build_small_event_embed(
                    description=description,
                    tx_hash=swap.tx_hash,
                )
                embed.color = color
            else:
                title = f"{emoji} {token} {action}"
                sea = await get_sea_creature_for_address(swap.owner)
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
                    unique_id=f"dex_trade_{swap.tx_hash}:{swap.log_index}",
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

    async def _fetch_balancer_swaps(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[DexSwap]:
        assert self._balancer_vault is not None
        assert self._tokens is not None

        swap_event = self._balancer_vault.events.Swap()
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
                        "address": self._balancer_vault.address,
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
            self._fetch_balancer_swaps,
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
