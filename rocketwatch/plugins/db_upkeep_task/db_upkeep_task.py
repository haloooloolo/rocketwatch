import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from datetime import timedelta
from typing import Any

import pymongo
from discord.ext import commands
from discord.utils import as_chunks
from eth_typing import BlockNumber, ChecksumAddress
from pymongo import UpdateOne
from pymongo.asynchronous.collection import AsyncCollection
from web3 import Web3
from web3.contract import AsyncContract
from web3.contract.async_contract import AsyncContractFunction

from rocketwatch.bot import RocketWatch
from rocketwatch.utils import solidity
from rocketwatch.utils.block_time import ts_to_block
from rocketwatch.utils.cronitor_monitor import AsyncMonitor
from rocketwatch.utils.event_logs import get_logs
from rocketwatch.utils.rocketpool import ValidatorInfo, at_address, rp
from rocketwatch.utils.shared_w3 import bacon, w3
from rocketwatch.utils.time_debug import timed

log = logging.getLogger("rocketwatch.db_upkeep_task")

# (contract_fn, require_success, transform, field_name)
MulticallSpec = tuple[AsyncContractFunction, bool, Callable[[Any], Any] | None, str]


def is_true(v: Any) -> bool:
    return isinstance(v, bool) and v


def safe_to_float(num: int) -> float | None:
    try:
        return float(solidity.to_float(num))
    except Exception:
        return None


def safe_to_hex(b: bytes) -> str | None:
    return f"0x{b.hex()}" if b else None


def safe_state_to_str(state: int) -> str | None:
    try:
        return solidity.mp_state_to_str(state)
    except Exception:
        return None


def safe_inv(num: int) -> float | None:
    try:
        return float(1 / solidity.to_float(num))
    except Exception:
        return None


def _parse_epoch(value: int) -> int | None:
    epoch = int(value)
    return epoch if epoch < 2**32 else None


def _derive_validator_status(info: ValidatorInfo) -> str:
    if info.dissolved:
        return "dissolved"
    if info.exited:
        return "exited"
    if info.in_queue:
        return "in_queue"
    if info.in_prestake:
        return "prestaked"
    if info.locked:
        return "locked"
    if info.exiting:
        return "exiting"
    if info.staked:
        return "staking"
    return "dequeued"


def _unpack_validator_info(info: ValidatorInfo) -> dict[str, Any]:
    return {
        "status": _derive_validator_status(info),
        "express_used": info.express_used,
        "exit_balance": solidity.to_float(info.exit_balance, 9),  # gwei to ETH
    }


def _unpack_validator_info_dynamic(info: ValidatorInfo) -> dict[str, Any]:
    return {
        "status": _derive_validator_status(info),
        "exit_balance": solidity.to_float(info.exit_balance, 9),
    }


def _withdrawal_address(record: dict[str, Any]) -> ChecksumAddress:
    credentials = record["validator"]["withdrawal_credentials"]
    return Web3.to_checksum_address(f"0x{credentials[-40:]}")


def _unpack_beacon_record(record: dict[str, Any]) -> dict[str, Any]:
    v = record["validator"]
    return {
        "validator_index": int(record["index"]),
        "beacon": {
            "status": record["status"],
            "balance": solidity.to_float(record["balance"], 9),
            "effective_balance": solidity.to_float(v["effective_balance"], 9),
            "slashed": v["slashed"],
            "activation_eligibility_epoch": _parse_epoch(
                v["activation_eligibility_epoch"]
            ),
            "activation_epoch": _parse_epoch(v["activation_epoch"]),
            "exit_epoch": _parse_epoch(v["exit_epoch"]),
            "withdrawable_epoch": _parse_epoch(v["withdrawable_epoch"]),
        },
    }


# beacon requests in flight per step; higher values barely speed this up
# but delay other beacon callers by seconds
_BATCH_CONCURRENCY = 4


async def _for_each_batch[T](
    batches: Iterable[list[T]], process: Callable[[int, list[T]], Awaitable[None]]
) -> None:
    """Run process(i, batch) for each batch, a few at a time."""
    sem = asyncio.Semaphore(_BATCH_CONCURRENCY)

    async def run(i: int, batch: list[T]) -> None:
        async with sem:
            await process(i, batch)

    try:
        async with asyncio.TaskGroup() as tg:
            for i, batch in enumerate(batches):
                tg.create_task(run(i, batch))
    except ExceptionGroup as eg:
        # surface the first failure as-is for error reporting
        raise eg.exceptions[0] from None


class _CachedCalls:
    """Builds each contract.functions.<name>(*args) call once, so per-item
    calls only need a cheap at_address rebind."""

    def __init__(self, contract: AsyncContract) -> None:
        self._contract = contract
        self._cache: dict[tuple[str, tuple[Any, ...]], AsyncContractFunction] = {}

    def __getattr__(self, name: str) -> Callable[..., AsyncContractFunction]:
        def build(*args: Any) -> AsyncContractFunction:
            key = (name, args)
            if (fn := self._cache.get(key)) is None:
                fn = self._contract.functions[name](*args)
                self._cache[key] = fn
            return fn

        return build


class DBUpkeepTask(commands.Cog):
    def __init__(self, bot: RocketWatch) -> None:
        self.bot = bot
        self.monitor = AsyncMonitor("db-task")
        self.beacon_batch_size = 250
        self.multicall_batch_size = 1000
        self.cooldown = timedelta(minutes=5)
        self.bot.loop.create_task(self.loop())

    async def loop(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(self.cooldown.total_seconds())
        await self.check_indexes()
        while not self.bot.is_closed():
            p_id = time.time()
            await self.monitor.ping(state="run", series=p_id)
            try:
                log.debug("starting db upkeep task")
                # node operator tasks
                await self.add_untracked_node_operators()
                await self.add_static_node_operator_data()
                await self.update_dynamic_node_operator_data()
                await self.update_dynamic_megapool_data()
                # minipool tasks
                await self.add_untracked_minipools()
                await self.add_static_minipool_data()
                await self.add_static_minipool_deposit_data()
                await self.update_dynamic_minipool_data()
                await self.update_dynamic_minipool_beacon_data()
                # megapool validator tasks
                await self.add_untracked_megapool_validators()
                await self.update_dynamic_megapool_validator_data()
                await self.update_dynamic_megapool_validator_beacon_data()
                log.debug("finished db upkeep task")
                await self.monitor.ping(state="complete", series=p_id)
            except Exception as err:
                await self.monitor.ping(state="fail", series=p_id)
                await self.bot.report_error(err)
            finally:
                await asyncio.sleep(self.cooldown.total_seconds())

    async def check_indexes(self) -> None:
        log.debug("checking indexes")
        await self.bot.db.node_operators.create_index("address")
        await self.bot.db.node_operators.create_index("megapool.address")
        await self.bot.db.minipools.create_index("address")
        await self.bot.db.minipools.create_index("pubkey")
        await self.bot.db.minipools.create_index("validator_index")
        await self.bot.db.minipools.create_index("beacon.status")
        await self.bot.db.megapool_validators.create_index(
            [("megapool", pymongo.ASCENDING), ("validator_id", pymongo.ASCENDING)],
            unique=True,
        )
        await self.bot.db.megapool_validators.create_index("pubkey")
        await self.bot.db.megapool_validators.create_index("validator_index")
        await self.bot.db.megapool_validators.create_index("status")
        await self.bot.db.megapool_validators.create_index("beacon.status")
        log.debug("indexes checked")

    async def _batch_multicall_update(
        self,
        collection: AsyncCollection[dict[str, Any]],
        query: dict[str, Any],
        call_fn: Callable[[dict[str, Any]], Coroutine[Any, Any, list[MulticallSpec]]],
        projection: dict[str, Any],
        label: str | None,
    ) -> None:
        items = await collection.find(query, projection).to_list()
        if not items:
            return

        total = len(items)
        first_calls = await call_fn(items[0])
        batch_size = self.multicall_batch_size // len(first_calls)
        for i, batch in enumerate(as_chunks(items, batch_size)):
            if label:
                start = i * batch_size + 1
                end = min((i + 1) * batch_size, total)
                log.debug(f"Processing {label} [{start}, {end}]/{total}")
            # call_fn(item) returns a list of (fn, require_success, transform, field)
            expanded = []
            for item in batch:
                for t in await call_fn(item):
                    expanded.append((item["address"], *t))
            calls = [(e[1], e[2]) for e in expanded]
            results = await rp.multicall(calls)
            updates: dict[Any, dict[str, Any]] = defaultdict(dict)
            for e, value in zip(expanded, results, strict=False):
                addr, transform, field = e[0], e[3], e[4]
                if transform is not None and value is not None:
                    value = transform(value)
                updates[addr][field] = value
            await collection.bulk_write(
                [
                    UpdateOne({"address": addr}, {"$set": d})
                    for addr, d in updates.items()
                ],
                ordered=False,
            )

    async def _update_beacon_data(
        self,
        collection: AsyncCollection[dict[str, Any]],
        query: dict[str, Any],
        owner_field: str,
        label: str,
    ) -> None:
        docs = await collection.find(
            {**query, "pubkey": {"$ne": None}}, {"pubkey": 1, owner_field: 1}
        ).to_list()
        if not docs:
            return
        total = len(docs)

        async def process(i: int, doc_batch: list[dict[str, Any]]) -> None:
            start = i * self.beacon_batch_size + 1
            end = min((i + 1) * self.beacon_batch_size, total)
            log.debug(f"Updating beacon data for {label} [{start}, {end}]/{total}")
            pubkeys = list({doc["pubkey"] for doc in doc_batch})
            response = await bacon.get_validators_by_ids("head", ids=pubkeys)

            # pubkeys get reused, so only the withdrawal credentials identify the owner
            records = {
                (r["validator"]["pubkey"], _withdrawal_address(r)): r
                for r in response["data"]
            }
            updates = []
            for doc in doc_batch:
                record = records.get((doc["pubkey"], doc[owner_field]))
                if record is not None:
                    data = _unpack_beacon_record(record)
                    updates.append(UpdateOne({"_id": doc["_id"]}, {"$set": data}))

            if updates:
                await collection.bulk_write(updates, ordered=False)

        await _for_each_batch(as_chunks(docs, self.beacon_batch_size), process)

    # -- Node operator tasks --

    @timed
    async def add_untracked_node_operators(self) -> None:
        nm = await rp.get_contract_by_name("rocketNodeManager")
        latest_rp = await rp.call("rocketNodeManager.getNodeCount") - 1
        latest_db = -1
        if res := await self.bot.db.node_operators.find_one(
            sort=[("_id", pymongo.DESCENDING)]
        ):
            latest_db = res["_id"]
        if latest_db >= latest_rp:
            log.debug("No new nodes")
            return
        data: dict[int, Any] = {}
        for index_batch in as_chunks(
            range(latest_db + 1, latest_rp + 1), self.beacon_batch_size
        ):
            results = await rp.multicall(
                [nm.functions.getNodeAt(i) for i in index_batch]
            )
            data |= dict(zip(index_batch, results, strict=False))
        await self.bot.db.node_operators.insert_many(
            [{"_id": i, "address": w3.to_checksum_address(a)} for i, a in data.items()]
        )

    @timed
    async def add_static_node_operator_data(self) -> None:
        df = await rp.get_contract_by_name("rocketNodeDistributorFactory")
        mf = await rp.get_contract_by_name("rocketMegapoolFactory")

        async def get_calls(n: dict[str, Any]) -> list[MulticallSpec]:
            return [
                (
                    df.functions.getProxyAddress(n["address"]),
                    True,
                    w3.to_checksum_address,
                    "fee_distributor.address",
                ),
                (
                    mf.functions.getExpectedAddress(n["address"]),
                    True,
                    w3.to_checksum_address,
                    "megapool.address",
                ),
            ]

        await self._batch_multicall_update(
            self.bot.db.node_operators,
            {
                "$or": [
                    {"fee_distributor.address": {"$exists": False}},
                    {"megapool.address": {"$exists": False}},
                ]
            },
            get_calls,
            {"address": 1},
            label="node operators",
        )

    @timed
    async def update_dynamic_node_operator_data(self) -> None:
        mf = await rp.get_contract_by_name("rocketMegapoolFactory")
        nd = await rp.get_contract_by_name("rocketNodeDeposit")
        nm = await rp.get_contract_by_name("rocketNodeManager")
        mm = await rp.get_contract_by_name("rocketMinipoolManager")
        ns = await rp.get_contract_by_name("rocketNodeStaking")
        mc = await rp.get_contract_by_name("multicall3")

        async def get_calls(n: dict[str, Any]) -> list[MulticallSpec]:
            return [
                (
                    nm.functions.getNodeWithdrawalAddress(n["address"]),
                    True,
                    w3.to_checksum_address,
                    "withdrawal_address",
                ),
                (
                    nm.functions.getNodeTimezoneLocation(n["address"]),
                    True,
                    None,
                    "timezone_location",
                ),
                (
                    nm.functions.getSmoothingPoolRegistrationState(n["address"]),
                    True,
                    None,
                    "smoothing_pool_registration",
                ),
                (
                    nm.functions.getAverageNodeFee(n["address"]),
                    True,
                    safe_to_float,
                    "average_node_fee",
                ),
                (
                    ns.functions.getNodeETHCollateralisationRatio(n["address"]),
                    True,
                    safe_inv,
                    "effective_node_share",
                ),
                (
                    mm.functions.getNodeStakingMinipoolCount(n["address"]),
                    True,
                    None,
                    "staking_minipool_count",
                ),
                (
                    nd.functions.getNodeDepositCredit(n["address"]),
                    True,
                    safe_to_float,
                    "node_credit",
                ),
                (
                    nd.functions.getNodeEthBalance(n["address"]),
                    True,
                    safe_to_float,
                    "node_eth_balance",
                ),
                (
                    nm.functions.getFeeDistributorInitialised(n["address"]),
                    True,
                    None,
                    "fee_distributor.initialized",
                ),
                (
                    mc.functions.getEthBalance(n["fee_distributor"]["address"]),
                    True,
                    safe_to_float,
                    "fee_distributor.eth_balance",
                ),
                (
                    mf.functions.getMegapoolDeployed(n["address"]),
                    True,
                    None,
                    "megapool.deployed",
                ),
                (
                    mc.functions.getEthBalance(n["megapool"]["address"]),
                    True,
                    safe_to_float,
                    "megapool.eth_balance",
                ),
                (
                    ns.functions.getNodeStakedRPL(n["address"]),
                    True,
                    safe_to_float,
                    "rpl.total_stake",
                ),
                (
                    ns.functions.getNodeLegacyStakedRPL(n["address"]),
                    True,
                    safe_to_float,
                    "rpl.legacy_stake",
                ),
                (
                    ns.functions.getNodeMegapoolStakedRPL(n["address"]),
                    True,
                    safe_to_float,
                    "rpl.megapool_stake",
                ),
                (
                    ns.functions.getNodeLockedRPL(n["address"]),
                    True,
                    safe_to_float,
                    "rpl.locked",
                ),
                (
                    ns.functions.getNodeUnstakingRPL(n["address"]),
                    True,
                    safe_to_float,
                    "rpl.unstaking",
                ),
                (
                    ns.functions.getNodeRPLStakedTime(n["address"]),
                    True,
                    None,
                    "rpl.last_stake_time",
                ),
                (
                    ns.functions.getNodeLastUnstakeTime(n["address"]),
                    True,
                    None,
                    "rpl.last_unstake_time",
                ),
            ]

        await self._batch_multicall_update(
            self.bot.db.node_operators,
            {},
            get_calls,
            label="node operators",
            projection={
                "address": 1,
                "fee_distributor.address": 1,
                "megapool.address": 1,
            },
        )

    @timed
    async def update_dynamic_megapool_data(self) -> None:
        mp = await rp.assemble_contract("rocketMegapoolDelegate")
        proxy = await rp.assemble_contract("rocketMegapoolProxy")
        mp_calls = _CachedCalls(mp)
        proxy_calls = _CachedCalls(proxy)

        async def get_calls(n: dict[str, Any]) -> list[MulticallSpec]:
            addr = n["megapool"]["address"]
            return [
                (
                    at_address(mp_calls.getValidatorCount(), addr),
                    True,
                    None,
                    "megapool.validator_count",
                ),
                (
                    at_address(mp_calls.getActiveValidatorCount(), addr),
                    True,
                    None,
                    "megapool.active_validator_count",
                ),
                (
                    at_address(mp_calls.getExitingValidatorCount(), addr),
                    True,
                    None,
                    "megapool.exiting_validator_count",
                ),
                (
                    at_address(mp_calls.getLockedValidatorCount(), addr),
                    True,
                    None,
                    "megapool.locked_validator_count",
                ),
                (
                    at_address(mp_calls.getNodeBond(), addr),
                    True,
                    safe_to_float,
                    "megapool.node_bond",
                ),
                (
                    at_address(mp_calls.getUserCapital(), addr),
                    True,
                    safe_to_float,
                    "megapool.user_capital",
                ),
                (
                    at_address(mp_calls.getNodeQueuedBond(), addr),
                    True,
                    safe_to_float,
                    "megapool.node_queued_bond",
                ),
                (
                    at_address(mp_calls.getDebt(), addr),
                    True,
                    safe_to_float,
                    "megapool.debt",
                ),
                (
                    at_address(mp_calls.getRefundValue(), addr),
                    True,
                    safe_to_float,
                    "megapool.refund_value",
                ),
                (
                    at_address(mp_calls.getPendingRewards(), addr),
                    True,
                    safe_to_float,
                    "megapool.pending_rewards",
                ),
                (
                    at_address(mp_calls.getLastDistributionTime(), addr),
                    True,
                    None,
                    "megapool.last_distribution_time",
                ),
                (
                    at_address(proxy_calls.getDelegate(), addr),
                    True,
                    w3.to_checksum_address,
                    "megapool.delegate",
                ),
                (
                    at_address(proxy_calls.getEffectiveDelegate(), addr),
                    True,
                    w3.to_checksum_address,
                    "megapool.effective_delegate",
                ),
                (
                    at_address(proxy_calls.getUseLatestDelegate(), addr),
                    True,
                    None,
                    "megapool.use_latest_delegate",
                ),
            ]

        await self._batch_multicall_update(
            self.bot.db.node_operators,
            {"megapool.deployed": True},
            get_calls,
            {"address": 1, "megapool.address": 1},
            label="megapools",
        )

    # -- Minipool tasks --

    @timed
    async def add_untracked_minipools(self) -> None:
        mm = await rp.get_contract_by_name("rocketMinipoolManager")
        latest_rp = await rp.call("rocketMinipoolManager.getMinipoolCount") - 1
        latest_db = -1
        if res := await self.bot.db.minipools.find_one(
            sort=[("_id", pymongo.DESCENDING)]
        ):
            latest_db = res["_id"]
        if latest_db >= latest_rp:
            log.debug("No new minipools")
            return
        log.debug(
            f"Latest minipool in db: {latest_db}, latest minipool in rp: {latest_rp}"
        )
        for index_batch in as_chunks(
            range(latest_db + 1, latest_rp + 1), self.beacon_batch_size
        ):
            results = await rp.multicall(
                [mm.functions.getMinipoolAt(i) for i in index_batch]
            )
            await self.bot.db.minipools.insert_many(
                [
                    {"_id": i, "address": w3.to_checksum_address(a)}
                    for i, a in zip(index_batch, results, strict=False)
                ]
            )

    @timed
    async def add_static_minipool_data(self) -> None:
        mm = await rp.get_contract_by_name("rocketMinipoolManager")
        minipool = await rp.assemble_contract("rocketMinipool")
        minipool_calls = _CachedCalls(minipool)

        async def lamb(n: dict[str, Any]) -> list[MulticallSpec]:
            return [
                (
                    at_address(minipool_calls.getNodeAddress(), n["address"]),
                    True,
                    w3.to_checksum_address,
                    "node_operator",
                ),
                (
                    mm.functions.getMinipoolPubkey(n["address"]),
                    True,
                    safe_to_hex,
                    "pubkey",
                ),
            ]

        await self._batch_multicall_update(
            self.bot.db.minipools,
            {"node_operator": {"$exists": False}},
            lamb,
            {"address": 1},
            label="minipools",
        )

    @timed
    async def add_static_minipool_deposit_data(self) -> None:
        minipools = (
            await self.bot.db.minipools.find(
                {"deposit_amount": {"$exists": False}, "status": "initialised"},
                {"address": 1, "_id": 0, "status_time": 1},
            )
            .sort("status_time", pymongo.ASCENDING)
            .to_list()
        )
        if not minipools:
            return
        nd = await rp.get_contract_by_name("rocketNodeDeposit")
        mm = await rp.get_contract_by_name("rocketMinipoolManager")

        for minipool_batch in as_chunks(minipools, self.beacon_batch_size):
            block_start = BlockNumber(
                await ts_to_block(minipool_batch[0]["status_time"]) - 1
            )
            block_end = BlockNumber(
                await ts_to_block(minipool_batch[-1]["status_time"]) + 1
            )
            log.debug(f"Processing deposit data for blocks {block_start}..{block_end}")
            addresses = {m["address"] for m in minipool_batch}

            events = await get_logs(
                nd.events.DepositReceived, block_start, block_end
            ) + await get_logs(mm.events.MinipoolCreated, block_start, block_end)
            events.sort(
                key=lambda e: (e["blockNumber"], e["transactionIndex"], e["logIndex"]),
                reverse=True,
            )

            # pair DepositReceived + MinipoolCreated events from same transaction
            pairs = []
            last_is_creation = False
            for e in events:
                if e["event"] == "MinipoolCreated":
                    if not last_is_creation:
                        pairs.append([e])
                    else:
                        pairs[-1] = [e]
                        log.info(
                            f"replacing creation event with newly found one ({pairs[-1]})"
                        )
                elif e["event"] == "DepositReceived" and last_is_creation:
                    pairs[-1].insert(0, e)
                last_is_creation = e["event"] == "MinipoolCreated"

            data = {}
            for pair in pairs:
                assert "amount" in pair[0]["args"]
                assert "minipool" in pair[1]["args"]
                assert pair[0]["transactionHash"] == pair[1]["transactionHash"]
                mp = str(pair[1]["args"]["minipool"]).lower()
                if mp in addresses:
                    data[mp] = {
                        "deposit_amount": solidity.to_float(pair[0]["args"]["amount"])
                    }

            if not data:
                continue
            await self.bot.db.minipools.bulk_write(
                [UpdateOne({"address": addr}, {"$set": d}) for addr, d in data.items()],
                ordered=False,
            )

    @timed
    async def update_dynamic_minipool_data(self) -> None:
        mc = await rp.get_contract_by_name("multicall3")
        minipool = await rp.assemble_contract("rocketMinipool")
        minipool_calls = _CachedCalls(minipool)

        async def get_calls(n: dict[str, Any]) -> list[MulticallSpec]:
            addr = n["address"]
            return [
                (
                    at_address(minipool_calls.getStatus(), addr),
                    True,
                    safe_state_to_str,
                    "status",
                ),
                (
                    at_address(minipool_calls.getStatusTime(), addr),
                    True,
                    None,
                    "status_time",
                ),
                (
                    at_address(minipool_calls.getVacant(), addr),
                    False,
                    is_true,
                    "vacant",
                ),
                (
                    at_address(minipool_calls.getFinalised(), addr),
                    True,
                    is_true,
                    "finalized",
                ),
                (
                    at_address(minipool_calls.getNodeDepositBalance(), addr),
                    True,
                    safe_to_float,
                    "node_deposit_balance",
                ),
                (
                    at_address(minipool_calls.getNodeRefundBalance(), addr),
                    True,
                    safe_to_float,
                    "node_refund_balance",
                ),
                (
                    at_address(minipool_calls.getPreMigrationBalance(), addr),
                    False,
                    safe_to_float,
                    "pre_migration_balance",
                ),
                (
                    at_address(minipool_calls.getNodeFee(), addr),
                    True,
                    safe_to_float,
                    "node_fee",
                ),
                (
                    at_address(minipool_calls.getDelegate(), addr),
                    True,
                    w3.to_checksum_address,
                    "delegate",
                ),
                (
                    at_address(minipool_calls.getPreviousDelegate(), addr),
                    False,
                    w3.to_checksum_address,
                    "previous_delegate",
                ),
                (
                    at_address(minipool_calls.getEffectiveDelegate(), addr),
                    True,
                    w3.to_checksum_address,
                    "effective_delegate",
                ),
                (
                    at_address(minipool_calls.getUseLatestDelegate(), addr),
                    True,
                    is_true,
                    "use_latest_delegate",
                ),
                (
                    at_address(minipool_calls.getUserDistributed(), addr),
                    False,
                    is_true,
                    "user_distributed",
                ),
                (
                    mc.functions.getEthBalance(addr),
                    True,
                    safe_to_float,
                    "execution_balance",
                ),
            ]

        await self._batch_multicall_update(
            self.bot.db.minipools,
            {"finalized": {"$ne": True}},
            get_calls,
            {"address": 1},
            label="minipools",
        )

    @timed
    async def update_dynamic_minipool_beacon_data(self) -> None:
        await self._update_beacon_data(
            self.bot.db.minipools,
            {"beacon.status": {"$ne": "withdrawal_done"}},
            owner_field="address",
            label="minipools",
        )

    # -- Megapool validator tasks --

    @timed
    async def add_untracked_megapool_validators(self) -> None:
        # get deployed megapools with their on-chain validator count
        nodes = await self.bot.db.node_operators.find(
            {"megapool.deployed": True, "megapool.validator_count": {"$gt": 0}},
            {"address": 1, "megapool.address": 1, "megapool.validator_count": 1},
        ).to_list()
        if not nodes:
            return

        for node in nodes:
            megapool_addr = node["megapool"]["address"]
            on_chain_count = node["megapool"]["validator_count"]
            db_count = await self.bot.db.megapool_validators.count_documents(
                {"megapool": megapool_addr}
            )
            if db_count >= on_chain_count:
                continue

            new_ids = list(range(db_count, on_chain_count))
            log.debug(
                f"Adding {len(new_ids)} new validators for megapool {megapool_addr}"
            )

            megapool_contract = await rp.assemble_contract(
                "rocketMegapoolDelegate", address=megapool_addr
            )
            for id_batch in as_chunks(new_ids, self.beacon_batch_size // 2):
                fns = [
                    fn
                    for vid in id_batch
                    for fn in [
                        megapool_contract.functions.getValidatorPubkey(vid),
                        megapool_contract.functions.getValidatorInfo(vid),
                    ]
                ]
                results = await rp.multicall(fns)

                docs = []
                for i, vid in enumerate(id_batch):
                    pubkey_raw = results[i * 2]
                    info_raw = results[i * 2 + 1]
                    doc = {
                        "megapool": megapool_addr,
                        "node_operator": node["address"],
                        "validator_id": vid,
                        "pubkey": safe_to_hex(pubkey_raw)
                        if pubkey_raw is not None
                        else None,
                    }
                    if info_raw is not None:
                        info = ValidatorInfo(*info_raw)
                        doc.update(_unpack_validator_info(info))
                    docs.append(doc)
                if docs:
                    await self.bot.db.megapool_validators.insert_many(
                        docs, ordered=False
                    )

    @timed
    async def update_dynamic_megapool_validator_data(self) -> None:
        mp = await rp.assemble_contract("rocketMegapoolDelegate")
        mp_calls = _CachedCalls(mp)

        validators = await self.bot.db.megapool_validators.find(
            {"status": {"$nin": ["exited", "dissolved", "dequeued"]}},
            {"megapool": 1, "validator_id": 1},
        ).to_list()
        if not validators:
            return

        total = len(validators)
        for i, batch in enumerate(as_chunks(validators, self.multicall_batch_size)):
            start = i * self.multicall_batch_size + 1
            end = min((i + 1) * self.multicall_batch_size, total)
            log.debug(f"Processing megapool validators [{start}, {end}]/{total}")
            fns = [
                at_address(mp_calls.getValidatorInfo(v["validator_id"]), v["megapool"])
                for v in batch
            ]
            results = await rp.multicall(fns)
            ops = []
            for v, info_raw in zip(batch, results, strict=False):
                if info_raw is not None:
                    info = ValidatorInfo(*info_raw)
                    ops.append(
                        UpdateOne(
                            {"_id": v["_id"]},
                            {"$set": _unpack_validator_info_dynamic(info)},
                        )
                    )
            if ops:
                await self.bot.db.megapool_validators.bulk_write(ops, ordered=False)

    @timed
    async def update_dynamic_megapool_validator_beacon_data(self) -> None:
        await self._update_beacon_data(
            self.bot.db.megapool_validators,
            # dequeued validators never reach the beacon chain
            {
                "beacon.status": {"$ne": "withdrawal_done"},
                "status": {"$ne": "dequeued"},
            },
            owner_field="megapool",
            label="megapool validators",
        )


async def setup(self: RocketWatch) -> None:
    await self.add_cog(DBUpkeepTask(self))
