import logging
import time
import asyncio
from collections import defaultdict

import pymongo
from multicall import Call
from cronitor import Monitor
from pymongo import AsyncMongoClient, UpdateOne, UpdateMany

from discord.ext import commands
from discord.utils import as_chunks

from rocketwatch import RocketWatch
from utils import solidity
from utils.cfg import cfg
from utils.block_time import ts_to_block
from utils.rocketpool import rp
from utils.shared_w3 import bacon
from utils.time_debug import timerun, timerun_async
from utils.event_logs import get_logs


log = logging.getLogger("db_upkeep_task")
log.setLevel(cfg["log_level"])

FAR_FUTURE_EPOCH = 2 ** 32


def safe_to_float(_, num):
    try:
        return solidity.to_float(num)
    except Exception:
        return None


def safe_to_hex(_, b):
    return f"0x{b.hex()}" if b else None


def safe_state_to_str(_, state):
    try:
        return solidity.mp_state_to_str(state)
    except Exception:
        return None


def safe_inv(_, num):
    try:
        return 1 / solidity.to_float(num)
    except Exception:
        return None


def is_true(_, b):
    return b is True


def _parse_epoch(value):
    epoch = int(value)
    return epoch if epoch < FAR_FUTURE_EPOCH else None


def _group_multicall_results(res):
    data = defaultdict(dict)
    for (key, field), value in res.items():
        data[key][field] = value
    return data


class DBUpkeepTask(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.db = AsyncMongoClient(cfg["mongodb.uri"]).rocketwatch
        self.monitor = Monitor("node-task", api_key=cfg["other.secrets.cronitor"])
        self.batch_size = 250
        self.bot.loop.create_task(self.loop())

    async def loop(self):
        await self.bot.wait_until_ready()
        await self.check_indexes()
        while not self.bot.is_closed():
            p_id = time.time()
            self.monitor.ping(state="run", series=p_id)
            try:
                log.debug("starting db upkeep task")
                # node tasks
                await self.add_untracked_node_operators()
                await self.add_static_data_to_node_operators()
                await self.update_dynamic_node_operator_metadata()
                # TODO: update megapool stats if deployed
                # minipool tasks
                await self.add_untracked_minipools()
                await self.add_static_data_to_minipools()
                await self.add_static_deposit_data_to_minipools()
                await self.add_static_beacon_data_to_minipools()
                await self.update_dynamic_minipool_metadata()
                await self.update_dynamic_minipool_beacon_metadata()
                # TODO: populate megapool validator DB
                log.debug("finished db upkeep task")
                self.monitor.ping(state="complete", series=p_id)
            except Exception as err:
                await self.bot.report_error(err)
                self.monitor.ping(state="fail", series=p_id)
            finally:
                await asyncio.sleep(600)

    async def check_indexes(self):
        log.debug("checking indexes")
        await self.db.minipools.create_index("address")
        await self.db.minipools.create_index("pubkey")
        await self.db.minipools.create_index("validator_index")
        await self.db.node_operators.create_index("address")
        log.debug("indexes checked")

    async def _batch_multicall_update(self, collection, query, lambs, label=None):
        addresses = await collection.distinct("address", query)
        if not addresses:
            return
        
        total = len(addresses)
        batch_size = self.batch_size // len(lambs)
        for i, batch in enumerate(as_chunks(addresses, batch_size)):
            if label:
                start = i * batch_size + 1
                end = min((i + 1) * batch_size, total)
                log.debug(f"Processing {label} [{start}, {end}]/{total}")
            res = await rp.multicall2(
                [Call(*lamb(a)) for a in batch for lamb in lambs],
                require_success=False
            )
            data = _group_multicall_results(res)
            await collection.bulk_write(
                [UpdateOne({"address": addr}, {"$set": d}) for addr, d in data.items()],
                ordered=False
            )

    # -- Node operator tasks --

    @timerun_async
    async def add_untracked_node_operators(self):
        nm = rp.get_contract_by_name("rocketNodeManager")
        latest_rp = rp.call("rocketNodeManager.getNodeCount") - 1
        latest_db = 0
        if res := await self.db.node_operators.find_one(sort=[("_id", pymongo.DESCENDING)]):
            latest_db = res["_id"]
        if latest_db >= latest_rp:
            log.debug("No new nodes")
            return
        data = {}
        for index_batch in as_chunks(range(latest_db + 1, latest_rp + 1), self.batch_size):
            data |= await rp.multicall2([
                Call(nm.address, [rp.seth_sig(nm.abi, "getNodeAt"), i], [(i, None)])
                for i in index_batch
            ])
        await self.db.node_operators.insert_many([{"_id": i, "address": a} for i, a in data.items()])

    @timerun_async
    async def add_static_data_to_node_operators(self):
        df = rp.get_contract_by_name("rocketNodeDistributorFactory")
        mf = rp.get_contract_by_name("rocketMegapoolFactory")
        lambs = [
            lambda a: (df.address, [rp.seth_sig(df.abi, "getProxyAddress"), a], [((a, "fee_distributor.address"), None)]),
            lambda a: (mf.address, [rp.seth_sig(mf.abi, "getExpectedAddress"), a], [((a, "megapool.address"), None)]),
        ]
        await self._batch_multicall_update(
            self.db.node_operators,
            {"$or": [{"fee_distributor.address": {"$exists": False}}, {"megapool.address": {"$exists": False}}]},
            lambs
        )

    @timerun_async
    async def update_dynamic_node_operator_metadata(self):
        mf = rp.get_contract_by_name("rocketMegapoolFactory")
        nd = rp.get_contract_by_name("rocketNodeDeposit")
        nm = rp.get_contract_by_name("rocketNodeManager")
        mm = rp.get_contract_by_name("rocketMinipoolManager")
        ns = rp.get_contract_by_name("rocketNodeStaking")
        mc = rp.get_contract_by_name("multicall3")
        lambs = [
            lambda n: (nm.address, [rp.seth_sig(nm.abi, "getNodeWithdrawalAddress"), n["address"]],
                       [((n["address"], "withdrawal_address"), None)]),
            lambda n: (nm.address, [rp.seth_sig(nm.abi, "getNodeTimezoneLocation"), n["address"]],
                       [((n["address"], "timezone_location"), None)]),
            lambda n: (nm.address, [rp.seth_sig(nm.abi, "getSmoothingPoolRegistrationState"), n["address"]],
                       [((n["address"], "smoothing_pool_registration"), None)]),
            lambda n: (nm.address, [rp.seth_sig(nm.abi, "getAverageNodeFee"), n["address"]],
                       [((n["address"], "average_node_fee"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeETHCollateralisationRatio"), n["address"]],
                       [((n["address"], "effective_node_share"), safe_inv)]),
            lambda n: (mm.address, [rp.seth_sig(mm.abi, "getNodeStakingMinipoolCount"), n["address"]],
                       [((n["address"], "staking_minipool_count"), None)]),
            lambda n: (nd.address, [rp.seth_sig(nd.abi, "getNodeDepositCredit"), n["address"]],
                       [((n["address"], "node_credit"), safe_to_float)]),
            lambda n: (nd.address, [rp.seth_sig(nd.abi, "getNodeEthBalance"), n["address"]],
                       [((n["address"], "node_eth_balance"), safe_to_float)]),
            lambda n: (nm.address, [rp.seth_sig(nm.abi, "getFeeDistributorInitialised"), n["address"]],
                       [((n["address"], "fee_distributor.initialized"), None)]),
            lambda n: (mc.address, [rp.seth_sig(mc.abi, "getEthBalance"), n["fee_distributor"]["address"]],
                       [((n["address"], "fee_distributor.eth_balance"), safe_to_float)]),
            lambda n: (nm.address, [rp.seth_sig(mf.abi, "getMegapoolDeployed"), n["address"]],
                       [((n["address"], "megapool.deployed"), is_true)]),
            lambda n: (mc.address, [rp.seth_sig(mc.abi, "getEthBalance"), n["megapool"]["address"]],
                       [((n["address"], "megapool.eth_balance"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeStakedRPL"), n["address"]],
                       [((n["address"], "rpl.total_stake"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeLegacyStakedRPL"), n["address"]],
                       [((n["address"], "rpl.legacy_stake"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeMegapoolStakedRPL"), n["address"]],
                       [((n["address"], "rpl.megapool_stake"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeLockedRPL"), n["address"]],
                       [((n["address"], "rpl.locked"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeUnstakingRPL"), n["address"]],
                       [((n["address"], "rpl.unstaking"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeRPLStakedTime"), n["address"]],
                       [((n["address"], "rpl.last_stake_time"), None)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeLastUnstakeTime"), n["address"]],
                       [((n["address"], "rpl.last_unstake_time"), None)])
        ]
        nodes = await self.db.node_operators.find(
            {}, {"address": 1, "fee_distributor.address": 1, "megapool.address": 1}
        ).to_list()
        total = len(nodes)
        batch_size = self.batch_size // len(lambs)
        for i, node_batch in enumerate(as_chunks(nodes, batch_size)):
            start = i * batch_size + 1
            end = min((i + 1) * batch_size, total)
            log.debug(f"Processing node operators [{start}, {end}]/{total}")
            res = await rp.multicall2(
                [Call(*lamb(n)) for n in node_batch for lamb in lambs],
                require_success=False
            )
            data = _group_multicall_results(res)
            await self.db.node_operators.bulk_write(
                [UpdateOne({"address": addr}, {"$set": d}) for addr, d in data.items()],
                ordered=False
            )

    # -- Minipool tasks --

    @timerun_async
    async def add_untracked_minipools(self):
        mm = rp.get_contract_by_name("rocketMinipoolManager")
        latest_rp = rp.call("rocketMinipoolManager.getMinipoolCount") - 1
        latest_db = 0
        if res := await self.db.minipools.find_one(sort=[("_id", pymongo.DESCENDING)]):
            latest_db = res["_id"]
        if latest_db >= latest_rp:
            log.debug("No new minipools")
            return
        log.debug(f"Latest minipool in db: {latest_db}, latest minipool in rp: {latest_rp}")
        for index_batch in as_chunks(range(latest_db + 1, latest_rp + 1), self.batch_size):
            data = await rp.multicall2([
                Call(mm.address, [rp.seth_sig(mm.abi, "getMinipoolAt"), i], [(i, None)])
                for i in index_batch
            ])
            await self.db.minipools.insert_many([{"_id": i, "address": a} for i, a in data.items()])

    @timerun_async
    async def add_static_data_to_minipools(self):
        m = rp.assemble_contract("rocketMinipool")
        mm = rp.get_contract_by_name("rocketMinipoolManager")
        lambs = [
            lambda a: (a, rp.seth_sig(m.abi, "getNodeAddress"), [((a, "node_operator"), None)]),
            lambda a: (mm.address, [rp.seth_sig(mm.abi, "getMinipoolPubkey"), a], [((a, "pubkey"), safe_to_hex)]),
        ]
        await self._batch_multicall_update(
            self.db.minipools,
            {"node_operator": {"$exists": False}},
            lambs
        )

    @timerun
    async def add_static_deposit_data_to_minipools(self):
        minipools = await self.db.minipools.find(
            {"deposit_amount": {"$exists": False}, "status": "initialised"},
            {"address": 1, "_id": 0, "status_time": 1}
        ).sort("status_time", pymongo.ASCENDING).to_list()
        if not minipools:
            return
        nd = rp.get_contract_by_name("rocketNodeDeposit")
        mm = rp.get_contract_by_name("rocketMinipoolManager")

        for minipool_batch in as_chunks(minipools, self.batch_size):
            block_start = ts_to_block(minipool_batch[0]["status_time"]) - 1
            block_end = ts_to_block(minipool_batch[-1]["status_time"]) + 1
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
            await self.db.minipools.bulk_write(
                [UpdateOne({"address": addr}, {"$set": d}) for addr, d in data.items()],
                ordered=False
            )

    @timerun
    async def add_static_beacon_data_to_minipools(self):
        public_keys = await self.db.minipools.distinct("pubkey", {"validator_index": {"$exists": False}})
        if not public_keys:
            return
        for pubkey_batch in as_chunks(public_keys, self.batch_size):
            beacon_data = (await bacon.get_validators_async("head", ids=pubkey_batch))["data"]
            data = {d["validator"]["pubkey"]: int(d["index"]) for d in beacon_data}
            await self.db.minipools.bulk_write(
                [UpdateMany({"pubkey": pk}, {"$set": {"validator_index": idx}}) for pk, idx in data.items()],
                ordered=False
            )

    @timerun_async
    async def update_dynamic_minipool_metadata(self):
        m = rp.assemble_contract("rocketMinipool")
        mc = rp.get_contract_by_name("multicall3")
        lambs = [
            lambda a: (a, rp.seth_sig(m.abi, "getStatus"), [((a, "status"), safe_state_to_str)]),
            lambda a: (a, rp.seth_sig(m.abi, "getStatusTime"), [((a, "status_time"), None)]),
            lambda a: (a, rp.seth_sig(m.abi, "getVacant"), [((a, "vacant"), is_true)]),
            lambda a: (a, rp.seth_sig(m.abi, "getNodeDepositBalance"), [((a, "node_deposit_balance"), safe_to_float)]),
            lambda a: (a, rp.seth_sig(m.abi, "getNodeRefundBalance"), [((a, "node_refund_balance"), safe_to_float)]),
            lambda a: (a, rp.seth_sig(m.abi, "getPreMigrationBalance"), [((a, "pre_migration_balance"), safe_to_float)]),
            lambda a: (a, rp.seth_sig(m.abi, "getNodeFee"), [((a, "node_fee"), safe_to_float)]),
            lambda a: (a, rp.seth_sig(m.abi, "getEffectiveDelegate"), [((a, "effective_delegate"), None)]),
            lambda a: (a, rp.seth_sig(m.abi, "getUseLatestDelegate"), [((a, "use_latest_delegate"), None)]),
            lambda a: (a, rp.seth_sig(m.abi, "getUserDistributed"), [((a, "user_distributed"), None)]),
            lambda a: (mc.address, [rp.seth_sig(mc.abi, "getEthBalance"), a], [((a, "execution_balance"), safe_to_float)])
        ]
        await self._batch_multicall_update(self.db.minipools, {"finalized": {"$ne": True}}, lambs, label="minipools")

    @timerun
    async def update_dynamic_minipool_beacon_metadata(self):
        validator_indexes = await self.db.minipools.distinct(
            "validator_index", {"beacon.status": {"$ne": "withdrawal_done"}}
        )
        validator_indexes = [i for i in validator_indexes if i is not None]
        total = len(validator_indexes)
        for i, index_batch in enumerate(as_chunks(validator_indexes, self.batch_size)):
            start = i * self.batch_size + 1
            end = min((i + 1) * self.batch_size, total)
            log.info(f"Updating beacon chain data for minipools [{start}, {end}]/{total}")
            beacon_data = (await bacon.get_validators_async("head", ids=index_batch))["data"]
            data = {}
            for d in beacon_data:
                v = d["validator"]
                data[int(d["index"])] = {"beacon": {
                    "status": d["status"],
                    "balance": solidity.to_float(d["balance"], 9),
                    "effective_balance": solidity.to_float(v["effective_balance"], 9),
                    "slashed": v["slashed"],
                    "activation_eligibility_epoch": _parse_epoch(v["activation_eligibility_epoch"]),
                    "activation_epoch": _parse_epoch(v["activation_epoch"]),
                    "exit_epoch": _parse_epoch(v["exit_epoch"]),
                    "withdrawable_epoch": _parse_epoch(v["withdrawable_epoch"]),
                }}
            await self.db.minipools.bulk_write(
                [UpdateMany({"validator_index": idx}, {"$set": d}) for idx, d in data.items()],
                ordered=False
            )


async def setup(self):
    await self.add_cog(DBUpkeepTask(self))
