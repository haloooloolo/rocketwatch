import logging
import time
import asyncio
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Optional

import pymongo
from cronitor import Monitor
from pymongo import UpdateOne, UpdateMany
from pymongo.asynchronous.collection import AsyncCollection

from discord.ext import commands
from discord.utils import as_chunks

from rocketwatch import RocketWatch
from utils import solidity
from utils.cfg import cfg
from utils.block_time import ts_to_block
from utils.rocketpool import rp
from utils.shared_w3 import w3, bacon
from utils.time_debug import timerun, timerun_async
from utils.event_logs import get_logs


log = logging.getLogger("db_upkeep_task")
log.setLevel(cfg.log_level)


def is_true(v) -> bool:
    return v is True


def safe_to_float(num):
    try:
        return solidity.to_float(num)
    except Exception:
        return None


def safe_to_hex(b):
    return f"0x{b.hex()}" if b else None


def safe_state_to_str(state):
    try:
        return solidity.mp_state_to_str(state)
    except Exception:
        return None


def safe_inv(num):
    try:
        return 1 / solidity.to_float(num)
    except Exception:
        return None


def _parse_epoch(value):
    epoch = int(value)
    return epoch if epoch < 2 ** 32 else None


def _derive_validator_status(info):
    if info[9]:   # dissolved
        return "dissolved"
    if info[5]:   # exited
        return "exited"
    if info[6]:   # inQueue
        return "in_queue"
    if info[7]:   # inPrestake
        return "prestaked"
    if info[11]:  # locked
        return "locked"
    if info[10]:  # exiting
        return "exiting"
    if info[4]:   # staked
        return "staking"
    return "unknown"


def _unpack_validator_info(info):
    if info is None:
        return None
    return {
        "status": _derive_validator_status(info),
        "express_used": info[8],
        "assignment_time": info[0],
        "requested_bond": info[2] / 1000,  # milliether to ETH
        "deposit_value": info[3] / 1000,  # milliether to ETH
        "exit_balance": solidity.to_float(info[12], 9),  # gwei to ETH
    }


def _unpack_validator_info_dynamic(info):
    if info is None:
        return None
    return {
        "status": _derive_validator_status(info),
        "assignment_time": info[0],
        "requested_bond": info[2] / 1000,
        "deposit_value": info[3] / 1000,
        "exit_balance": solidity.to_float(info[12], 9),
    }


class DBUpkeepTask(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.monitor = Monitor("db-task", api_key=cfg.other.secrets.cronitor)
        self.batch_size = 250
        self.cooldown = timedelta(minutes=10)
        self.bot.loop.create_task(self.loop())

    async def loop(self):
        await self.bot.wait_until_ready()
        await self.check_indexes()
        while not self.bot.is_closed():
            p_id = time.time()
            self.monitor.ping(state="run", series=p_id)
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
                self.monitor.ping(state="complete", series=p_id)
            except Exception as err:
                self.monitor.ping(state="fail", series=p_id)
                await self.bot.report_error(err)
            finally:
                await asyncio.sleep(self.cooldown.total_seconds())

    async def check_indexes(self):
        log.debug("checking indexes")
        await self.bot.db.node_operators.create_index("address")
        await self.bot.db.node_operators.create_index("megapool.address")
        await self.bot.db.minipools.create_index("address")
        await self.bot.db.minipools.create_index("pubkey")
        await self.bot.db.minipools.create_index("validator_index")
        await self.bot.db.minipools.create_index("beacon.status")
        await self.bot.db.megapool_validators.create_index(
            [("megapool", pymongo.ASCENDING), ("validator_id", pymongo.ASCENDING)], unique=True
        )
        await self.bot.db.megapool_validators.create_index("pubkey")
        await self.bot.db.megapool_validators.create_index("validator_index")
        await self.bot.db.megapool_validators.create_index("beacon.status")
        log.debug("indexes checked")

    async def _batch_multicall_update(
        self,
        collection: AsyncCollection,
        query: dict[str, Any],
        call_fn: Callable[[dict[str, Any]], list[tuple]],
        projection: dict[str, Any],
        label: Optional[str],
    ) -> None:
        items = await collection.find(query, projection).to_list()
        if not items:
            return

        total = len(items)
        first_calls = await call_fn(items[0]) if asyncio.iscoroutinefunction(call_fn) else call_fn(items[0])
        batch_size = self.batch_size // len(first_calls)
        for i, batch in enumerate(as_chunks(items, batch_size)):
            if label:
                start = i * batch_size + 1
                end = min((i + 1) * batch_size, total)
                log.debug(f"Processing {label} [{start}, {end}]/{total}")
            # call_fn(item) returns a list of (fn, require_success, transform, field)
            if asyncio.iscoroutinefunction(call_fn):
                expanded = []
                for item in batch:
                    for t in await call_fn(item):
                        expanded.append((item["address"], *t))
            else:
                expanded = [(item["address"], *t) for item in batch for t in call_fn(item)]
            calls = [(e[1], e[2]) for e in expanded]
            results = await rp.multicall(calls)
            updates = defaultdict(dict)
            for e, value in zip(expanded, results):
                addr, transform, field = e[0], e[3], e[4]
                if transform is not None and value is not None:
                    value = transform(value)
                updates[addr][field] = value
            await collection.bulk_write(
                [UpdateOne({"address": addr}, {"$set": d}) for addr, d in updates.items()],
                ordered=False
            )

    # -- Node operator tasks --

    @timerun_async
    async def add_untracked_node_operators(self):
        nm = await rp.get_contract_by_name("rocketNodeManager")
        latest_rp = await rp.call("rocketNodeManager.getNodeCount") - 1
        latest_db = 0
        if res := await self.bot.db.node_operators.find_one(sort=[("_id", pymongo.DESCENDING)]):
            latest_db = res["_id"]
        if latest_db >= latest_rp:
            log.debug("No new nodes")
            return
        data = {}
        for index_batch in as_chunks(range(latest_db + 1, latest_rp + 1), self.batch_size):
            results = await rp.multicall([nm.functions.getNodeAt(i) for i in index_batch])
            data |= dict(zip(index_batch, results))
        await self.bot.db.node_operators.insert_many(
            [{"_id": i, "address": w3.to_checksum_address(a)} for i, a in data.items()]
        )

    @timerun_async
    async def add_static_node_operator_data(self):
        df = await rp.get_contract_by_name("rocketNodeDistributorFactory")
        mf = await rp.get_contract_by_name("rocketMegapoolFactory")

        def get_calls(n):
            return [
                (df.functions.getProxyAddress(n["address"]), True, w3.to_checksum_address, "fee_distributor.address"),
                (mf.functions.getExpectedAddress(n["address"]), True, w3.to_checksum_address, "megapool.address"),
            ]
        await self._batch_multicall_update(
            self.bot.db.node_operators,
            {"$or": [{"fee_distributor.address": {"$exists": False}}, {"megapool.address": {"$exists": False}}]},
            get_calls, {"address": 1}, label="node operators"
        )

    @timerun_async
    async def update_dynamic_node_operator_data(self):
        mf = await rp.get_contract_by_name("rocketMegapoolFactory")
        nd = await rp.get_contract_by_name("rocketNodeDeposit")
        nm = await rp.get_contract_by_name("rocketNodeManager")
        mm = await rp.get_contract_by_name("rocketMinipoolManager")
        ns = await rp.get_contract_by_name("rocketNodeStaking")
        mc = await rp.get_contract_by_name("multicall3")

        def get_calls(n):
            return [
                (nm.functions.getNodeWithdrawalAddress(n["address"]), True, w3.to_checksum_address, "withdrawal_address"),
                (nm.functions.getNodeTimezoneLocation(n["address"]), True, None, "timezone_location"),
                (nm.functions.getSmoothingPoolRegistrationState(n["address"]), True, None, "smoothing_pool_registration"),
                (nm.functions.getAverageNodeFee(n["address"]), True, safe_to_float, "average_node_fee"),
                (ns.functions.getNodeETHCollateralisationRatio(n["address"]), True, safe_inv, "effective_node_share"),
                (mm.functions.getNodeStakingMinipoolCount(n["address"]), True, None, "staking_minipool_count"),
                (nd.functions.getNodeDepositCredit(n["address"]), True, safe_to_float, "node_credit"),
                (nd.functions.getNodeEthBalance(n["address"]), True, safe_to_float, "node_eth_balance"),
                (nm.functions.getFeeDistributorInitialised(n["address"]), True, None, "fee_distributor.initialized"),
                (mc.functions.getEthBalance(n["fee_distributor"]["address"]),
                 True, safe_to_float, "fee_distributor.eth_balance"),
                (mf.functions.getMegapoolDeployed(n["address"]), True, None, "megapool.deployed"),
                (mc.functions.getEthBalance(n["megapool"]["address"]), True, safe_to_float, "megapool.eth_balance"),
                (ns.functions.getNodeStakedRPL(n["address"]), True, safe_to_float, "rpl.total_stake"),
                (ns.functions.getNodeLegacyStakedRPL(n["address"]), True, safe_to_float, "rpl.legacy_stake"),
                (ns.functions.getNodeMegapoolStakedRPL(n["address"]), True, safe_to_float, "rpl.megapool_stake"),
                (ns.functions.getNodeLockedRPL(n["address"]), True, safe_to_float, "rpl.locked"),
                (ns.functions.getNodeUnstakingRPL(n["address"]), True, safe_to_float, "rpl.unstaking"),
                (ns.functions.getNodeRPLStakedTime(n["address"]), True, None, "rpl.last_stake_time"),
                (ns.functions.getNodeLastUnstakeTime(n["address"]), True, None, "rpl.last_unstake_time"),
            ]
        await self._batch_multicall_update(
            self.bot.db.node_operators, {}, get_calls, label="node operators",
            projection={"address": 1, "fee_distributor.address": 1, "megapool.address": 1}
        )

    @timerun_async
    async def update_dynamic_megapool_data(self):
        async def get_calls(n):
            mp = await rp.assemble_contract("rocketMegapoolDelegate", address=n["megapool"]["address"])
            proxy = await rp.assemble_contract("rocketMegapoolProxy", address=n["megapool"]["address"])
            return [
                (mp.functions.getValidatorCount(), True, None, "megapool.validator_count"),
                (mp.functions.getActiveValidatorCount(), True, None, "megapool.active_validator_count"),
                (mp.functions.getExitingValidatorCount(), True, None, "megapool.exiting_validator_count"),
                (mp.functions.getLockedValidatorCount(), True, None, "megapool.locked_validator_count"),
                (mp.functions.getNodeBond(), True, safe_to_float, "megapool.node_bond"),
                (mp.functions.getUserCapital(), True, safe_to_float, "megapool.user_capital"),
                (mp.functions.getDebt(), True, safe_to_float, "megapool.debt"),
                (mp.functions.getRefundValue(), True, safe_to_float, "megapool.refund_value"),
                (mp.functions.getPendingRewards(), True, safe_to_float, "megapool.pending_rewards"),
                (mp.functions.getLastDistributionTime(), True, None, "megapool.last_distribution_time"),
                (proxy.functions.getDelegate(), True, w3.to_checksum_address, "megapool.delegate"),
                (proxy.functions.getEffectiveDelegate(), True, w3.to_checksum_address, "megapool.effective_delegate"),
                (proxy.functions.getUseLatestDelegate(), True, None, "megapool.use_latest_delegate"),
            ]
        await self._batch_multicall_update(
            self.bot.db.node_operators, {"megapool.deployed": True},
            get_calls, {"address": 1, "megapool.address": 1},
            label="megapools"
        )

    # -- Minipool tasks --

    @timerun_async
    async def add_untracked_minipools(self):
        mm = await rp.get_contract_by_name("rocketMinipoolManager")
        latest_rp = await rp.call("rocketMinipoolManager.getMinipoolCount") - 1
        latest_db = 0
        if res := await self.bot.db.minipools.find_one(sort=[("_id", pymongo.DESCENDING)]):
            latest_db = res["_id"]
        if latest_db >= latest_rp:
            log.debug("No new minipools")
            return
        log.debug(f"Latest minipool in db: {latest_db}, latest minipool in rp: {latest_rp}")
        for index_batch in as_chunks(range(latest_db + 1, latest_rp + 1), self.batch_size):
            results = await rp.multicall([mm.functions.getMinipoolAt(i) for i in index_batch])
            await self.bot.db.minipools.insert_many(
                [{"_id": i, "address": w3.to_checksum_address(a)} for i, a in zip(index_batch, results)]
            )

    @timerun_async
    async def add_static_minipool_data(self):
        mm = await rp.get_contract_by_name("rocketMinipoolManager")

        async def lamb(n):
            return [
                (
                    (await rp.assemble_contract("rocketMinipool", address=n["address"]))
                    .functions.getNodeAddress(), True, w3.to_checksum_address, "node_operator"
                ),
                (mm.functions.getMinipoolPubkey(n["address"]), True, safe_to_hex, "pubkey"),
            ]
        await self._batch_multicall_update(
            self.bot.db.minipools,
            {"node_operator": {"$exists": False}},
            lamb, {"address": 1}, label="minipools"
        )

    @timerun
    async def add_static_minipool_deposit_data(self):
        minipools = await self.bot.db.minipools.find(
            {"deposit_amount": {"$exists": False}, "status": "initialised"},
            {"address": 1, "_id": 0, "status_time": 1}
        ).sort("status_time", pymongo.ASCENDING).to_list()
        if not minipools:
            return
        nd = await rp.get_contract_by_name("rocketNodeDeposit")
        mm = await rp.get_contract_by_name("rocketMinipoolManager")

        for minipool_batch in as_chunks(minipools, self.batch_size):
            block_start = await ts_to_block(minipool_batch[0]["status_time"]) - 1
            block_end = await ts_to_block(minipool_batch[-1]["status_time"]) + 1
            log.debug(f"Processing deposit data for blocks {block_start}..{block_end}")
            addresses = {m["address"] for m in minipool_batch}

            events = get_logs(nd.events.DepositReceived, block_start, block_end) \
                + get_logs(mm.events.MinipoolCreated, block_start, block_end)
            events.sort(key=lambda e: (e['blockNumber'], e['transactionIndex'], e['logIndex']), reverse=True)

            # pair DepositReceived + MinipoolCreated events from same transaction
            pairs = []
            last_is_creation = False
            for e in events:
                if e["event"] == "MinipoolCreated":
                    if not last_is_creation:
                        pairs.append([e])
                    else:
                        pairs[-1] = [e]
                        log.info(f"replacing creation event with newly found one ({pairs[-1]})")
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
                    data[mp] = {"deposit_amount": solidity.to_float(pair[0]["args"]["amount"])}

            if not data:
                continue
            await self.bot.db.minipools.bulk_write(
                [UpdateOne({"address": addr}, {"$set": d}) for addr, d in data.items()],
                ordered=False
            )

    @timerun_async
    async def update_dynamic_minipool_data(self):
        mc = await rp.get_contract_by_name("multicall3")

        async def get_calls(n):
            minipool_contract = await rp.assemble_contract("rocketMinipool", address=n["address"])
            return [
                (minipool_contract.functions.getStatus(), True, safe_state_to_str, "status"),
                (minipool_contract.functions.getStatusTime(), True, None, "status_time"),
                (minipool_contract.functions.getVacant(), False, is_true, "vacant"),
                (minipool_contract.functions.getFinalised(), True, is_true, "finalized"),
                (minipool_contract.functions.getNodeDepositBalance(), True, safe_to_float, "node_deposit_balance"),
                (minipool_contract.functions.getNodeRefundBalance(), True, safe_to_float, "node_refund_balance"),
                (minipool_contract.functions.getPreMigrationBalance(), False, safe_to_float, "pre_migration_balance"),
                (minipool_contract.functions.getNodeFee(), True, safe_to_float, "node_fee"),
                (minipool_contract.functions.getDelegate(), True, w3.to_checksum_address, "delegate"),
                (minipool_contract.functions.getPreviousDelegate(), False, w3.to_checksum_address, "previous_delegate"),
                (minipool_contract.functions.getEffectiveDelegate(), True, w3.to_checksum_address, "effective_delegate"),
                (minipool_contract.functions.getUseLatestDelegate(), True, is_true, "use_latest_delegate"),
                (minipool_contract.functions.getUserDistributed(), False, is_true, "user_distributed"),
                (mc.functions.getEthBalance(n["address"]), True, safe_to_float, "execution_balance"),
            ]
        await self._batch_multicall_update(
            self.bot.db.minipools, {"finalized": {"$ne": True}}, get_calls, {"address": 1}, label="minipools"
        )

    @timerun
    async def update_dynamic_minipool_beacon_data(self):
        pubkeys = await self.bot.db.minipools.distinct(
            "pubkey", {"beacon.status": {"$ne": "withdrawal_done"}}
        )
        pubkeys = [pk for pk in pubkeys if pk is not None]
        total = len(pubkeys)
        for i, pubkey_batch in enumerate(as_chunks(pubkeys, self.batch_size)):
            start = i * self.batch_size + 1
            end = min((i + 1) * self.batch_size, total)
            log.info(f"Updating beacon chain data for minipools [{start}, {end}]/{total}")
            beacon_data = (await bacon.get_validators_by_ids("head", ids=pubkey_batch))["data"]
            data = {}
            for d in beacon_data:
                v = d["validator"]
                data[v["pubkey"]] = {
                    "validator_index": int(d["index"]),
                    "beacon": {
                        "status": d["status"],
                        "balance": solidity.to_float(d["balance"], 9),
                        "effective_balance": solidity.to_float(v["effective_balance"], 9),
                        "slashed": v["slashed"],
                        "activation_eligibility_epoch": _parse_epoch(v["activation_eligibility_epoch"]),
                        "activation_epoch": _parse_epoch(v["activation_epoch"]),
                        "exit_epoch": _parse_epoch(v["exit_epoch"]),
                        "withdrawable_epoch": _parse_epoch(v["withdrawable_epoch"]),
                    },
                }
            if data:
                await self.bot.db.minipools.bulk_write(
                    [UpdateMany({"pubkey": pk}, {"$set": d}) for pk, d in data.items()],
                    ordered=False
                )

    # -- Megapool validator tasks --

    @timerun_async
    async def add_untracked_megapool_validators(self):
        # get deployed megapools with their on-chain validator count
        nodes = await self.bot.db.node_operators.find(
            {"megapool.deployed": True, "megapool.validator_count": {"$gt": 0}},
            {"address": 1, "megapool.address": 1, "megapool.validator_count": 1}
        ).to_list()
        if not nodes:
            return

        for node in nodes:
            megapool_addr = node["megapool"]["address"]
            on_chain_count = node["megapool"]["validator_count"]
            db_count = await self.bot.db.megapool_validators.count_documents({"megapool": megapool_addr})
            if db_count >= on_chain_count:
                continue

            new_ids = list(range(db_count, on_chain_count))
            log.debug(f"Adding {len(new_ids)} new validators for megapool {megapool_addr}")

            megapool_contract = await rp.assemble_contract("rocketMegapoolDelegate", address=megapool_addr)
            for id_batch in as_chunks(new_ids, self.batch_size // 2):
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
                        "pubkey": safe_to_hex(pubkey_raw) if pubkey_raw is not None else None,
                    }
                    info = _unpack_validator_info(info_raw)
                    if info:
                        doc.update(info)
                    docs.append(doc)
                if docs:
                    await self.bot.db.megapool_validators.insert_many(docs, ordered=False)

    @timerun_async
    async def update_dynamic_megapool_validator_data(self):
        validators = await self.bot.db.megapool_validators.find(
            {"status": {"$nin": ["exited", "dissolved"]}},
            {"megapool": 1, "validator_id": 1}
        ).to_list()
        if not validators:
            return

        total = len(validators)
        for i, batch in enumerate(as_chunks(validators, self.batch_size)):
            start = i * self.batch_size + 1
            end = min((i + 1) * self.batch_size, total)
            log.debug(f"Processing megapool validators [{start}, {end}]/{total}")
            fns = [
                (await rp.assemble_contract("rocketMegapoolDelegate", address=v["megapool"]))
                .functions.getValidatorInfo(v["validator_id"])
                for v in batch
            ]
            results = await rp.multicall(fns)
            ops = []
            for v, info_raw in zip(batch, results):
                info = _unpack_validator_info_dynamic(info_raw) if info_raw is not None else None
                if info is not None:
                    ops.append(UpdateOne({"_id": v["_id"]}, {"$set": info}))
            if ops:
                await self.bot.db.megapool_validators.bulk_write(ops, ordered=False)

    @timerun
    async def update_dynamic_megapool_validator_beacon_data(self):
        pubkeys = await self.bot.db.megapool_validators.distinct(
            "pubkey", {"beacon.status": {"$ne": "withdrawal_done"}}
        )
        pubkeys = [pk for pk in pubkeys if pk is not None]
        if not pubkeys:
            return
        total = len(pubkeys)
        for i, pubkey_batch in enumerate(as_chunks(pubkeys, self.batch_size)):
            start = i * self.batch_size + 1
            end = min((i + 1) * self.batch_size, total)
            log.debug(f"Updating beacon data for megapool validators [{start}, {end}]/{total}")
            beacon_data = (await bacon.get_validators_by_ids("head", ids=pubkey_batch))["data"]
            data = {}
            for d in beacon_data:
                v = d["validator"]
                data[v["pubkey"]] = {
                    "validator_index": int(d["index"]),
                    "beacon": {
                        "status": d["status"],
                        "balance": solidity.to_float(d["balance"], 9),
                        "effective_balance": solidity.to_float(v["effective_balance"], 9),
                        "slashed": v["slashed"],
                        "activation_eligibility_epoch": _parse_epoch(v["activation_eligibility_epoch"]),
                        "activation_epoch": _parse_epoch(v["activation_epoch"]),
                        "exit_epoch": _parse_epoch(v["exit_epoch"]),
                        "withdrawable_epoch": _parse_epoch(v["withdrawable_epoch"]),
                    },
                }
            if data:
                await self.bot.db.megapool_validators.bulk_write(
                    [UpdateMany({"pubkey": pk}, {"$set": d}) for pk, d in data.items()],
                    ordered=False
                )


async def setup(self):
    await self.add_cog(DBUpkeepTask(self))
