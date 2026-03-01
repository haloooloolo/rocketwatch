import logging
import time
import asyncio

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


def safe_to_float(_, num: int):
    try:
        return solidity.to_float(num)
    except Exception:
        return None

def safe_to_hex(_, b: bytes):
    return f"0x{b.hex()}" if b else None

def safe_state_to_str(_, state: int):
    try:
        return solidity.mp_state_to_str(state)
    except Exception:
        return None

def safe_inv(_, num: int):
    try:
        return 1 / solidity.to_float(num)
    except Exception:
        return None

def is_true(_, b):
    return b is True


class DBUpkeepTask(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.db = AsyncMongoClient(cfg["mongodb.uri"]).rocketwatch
        self.monitor = Monitor("node-task", api_key=cfg["other.secrets.cronitor"])
        self.batch_size = 50
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
                # minipool tasks
                await self.add_untracked_minipools()
                await self.add_static_data_to_minipools()
                await self.add_static_deposit_data_to_minipools()
                await self.add_static_beacon_data_to_minipools()
                await self.update_dynamic_minipool_metadata()
                await self.update_dynamic_minipool_beacon_metadata()
                log.debug("finished db upkeep task")
                self.monitor.ping(state="complete", series=p_id)
            except Exception as err:
                await self.bot.report_error(err)
                self.monitor.ping(state="fail", series=p_id)
            finally:
                await asyncio.sleep(600)

    @timerun_async
    async def add_untracked_minipools(self):
        # rocketMinipoolManager.getMinipoolAt(i) returns the address of the minipool at index i
        mm = rp.get_contract_by_name("rocketMinipoolManager")
        latest_rp = rp.call("rocketMinipoolManager.getMinipoolCount") - 1
        # get latest _id in minipools collection
        latest_db = 0
        if res := await self.db.minipools.find_one(sort=[("_id", pymongo.DESCENDING)]):
            latest_db = res["_id"]
        # return early if we're up to date
        if latest_db >= latest_rp:
            log.debug("No new minipools")
            return
        
        log.debug(f"Latest minipool in db: {latest_db}, latest minipool in rp: {latest_rp}")
        # batch into self.batch_size minipools at a time, between latest_id and minipool_count
        for index_batch in as_chunks(range(latest_db + 1, latest_rp + 1), self.batch_size):
            data = await rp.multicall2([
                Call(mm.address, [rp.seth_sig(mm.abi, "getMinipoolAt"), i], [(i, None)])
                for i in index_batch
            ])
            log.debug(f"Inserting {len(data)} new minipools into db")
            await self.db.minipools.insert_many([
                {"_id": i, "address": a}
                for i, a in data.items()
            ])
            
        log.debug("New minipools inserted")

    @timerun_async
    async def add_static_data_to_minipools(self):
        m = rp.assemble_contract("rocketMinipool")
        mm = rp.get_contract_by_name("rocketMinipoolManager")
        lambs = [
            lambda a: (a, rp.seth_sig(m.abi, "getNodeAddress"), [((a, "node_operator"), None)]),
            lambda a: (mm.address, [rp.seth_sig(mm.abi, "getMinipoolPubkey"), a], [((a, "pubkey"), safe_to_hex)]),
        ]
        # get all minipool addresses from db that do not have a node operator assigned
        minipool_addresses = await self.db.minipools.distinct("address", {"node_operator": {"$exists": False}})
        # get node operator addresses from rp
        # return early if no minipools need to be updated
        if not minipool_addresses:
            log.debug("No minipools need to be updated with static data")
            return
        
        for minipool_batch in as_chunks(minipool_addresses, self.batch_size // len(lambs)):
            data = {}
            res = await rp.multicall2(
                [Call(*lamb(a)) for a in minipool_batch for lamb in lambs], 
                require_success=False
            )
            # update data dict with results
            for (address, variable_name), value in res.items():
                if address not in data:
                    data[address] = {}
                data[address][variable_name] = value
            log.debug(f"Updating {len(data)} minipools with static data")
            # update minipools in db
            bulk = [
                UpdateOne(
                    {"address": a},
                    {"$set": d},
                ) for a, d in data.items()
            ]
            await self.db.minipools.bulk_write(bulk, ordered=False)
        log.debug("Minipools updated with static data")

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
        # get all minipool addresses from db
        minipool_addresses = await self.db.minipools.distinct("address", {"finalized": {"$ne": True}})
        for minipool_batch in as_chunks(minipool_addresses, self.batch_size // len(lambs)):
            res = await rp.multicall2(
                [Call(*lamb(a)) for a in minipool_batch for lamb in lambs], 
                require_success=False
            )
            # update data dict with results
            data = {}
            for (address, variable_name), value in res.items():
                if address not in data:
                    data[address] = {}
                data[address][variable_name] = value
            # update minipools in db
            log.debug(f"Updating {len(res)} minipool attributes in db")
            bulk = [
                UpdateOne(
                    {"address": a},
                    {"$set": d}
                ) for a, d in data.items()
            ]
            await self.db.minipools.bulk_write(bulk, ordered=False)
                
        log.debug("Minipools updated with metadata")

    @timerun
    async def add_static_deposit_data_to_minipools(self):
        # get all minipool addresses and their status time from db that :
        # - do not have a deposit_amount
        # - are in the initialized state
        # sort by status time
        minipools = await self.db.minipools.find(
            {"deposit_amount": {"$exists": False}, "status": "initialised"},
            {"address": 1, "_id": 0, "status_time": 1}
        ).sort("status_time", pymongo.ASCENDING).to_list()
        # return early if no minipools need to be updated
        if not minipools:
            log.debug("No minipools need to be updated with static deposit data")
            return
        nd = rp.get_contract_by_name("rocketNodeDeposit")
        mm = rp.get_contract_by_name("rocketMinipoolManager")
        
        for minipool_batch in as_chunks(minipools, self.batch_size):
            # turn status time of first and last minipool into blocks
            block_start = ts_to_block(minipool_batch[0]["status_time"]) - 1
            block_end = ts_to_block(minipool_batch[-1]["status_time"]) + 1
            a = [m["address"] for m in minipool_batch]
            
            f_deposits = get_logs(nd.events.DepositReceived, block_start, block_end)
            f_creations = get_logs(mm.events.MinipoolCreated, block_start, block_end)
            events = f_deposits + f_creations
            
            events = sorted(events, key=lambda x: (x['blockNumber'], x['transactionIndex'], x['logIndex'] *1e-8), reverse=True)
            # map to pairs of 2
            prepared_events = []
            last_addition_is_creation = False
            
            while events:
                # get event
                e = events.pop(0)
                if e["event"] == "MinipoolCreated":
                    if not last_addition_is_creation:
                        prepared_events.append([e])
                    else: 
                        prepared_events[-1] = [e]
                        log.info(f"replacing creation even with newly found one ({prepared_events[-1]})")
                elif e["event"] == "DepositReceived" and last_addition_is_creation:
                    prepared_events[-1].insert(0, e)
                last_addition_is_creation = e["event"] == "MinipoolCreated"
                
            data = {}
            for e in prepared_events:
                assert "amount" in e[0]["args"]
                assert "minipool" in e[1]["args"]
                # assert that the txn hashes match
                assert e[0]["transactionHash"] == e[1]["transactionHash"]
                mp = str(e[1]["args"]["minipool"]).lower()
                if mp not in a:
                    continue
                amount = solidity.to_float(e[0]["args"]["amount"])
                data[mp] = {"deposit_amount": amount}
        
            if not data:
                log.debug("No minipools need to be updated with static deposit data")
                continue
            
            log.debug(f"Updating {len(data)} minipools with static deposit data")
            # update minipools in db
            bulk = [
                UpdateOne(
                    {"address": a},
                    {"$set": d},
                ) for a, d in data.items()
            ]
            await self.db.minipools.bulk_write(bulk, ordered=False)
                
        log.debug("Minipools updated with static deposit data")

    @timerun
    async def add_static_beacon_data_to_minipools(self):
        # get all public keys from db where no validator_index is set
        public_keys = await self.db.minipools.distinct("pubkey", {"validator_index": {"$exists": False}})
        # return early if no minipools need to be updated
        if not public_keys:
            log.debug("No minipools need to be updated with static beacon data")
            return
        
        # we need to do smaller bulks as the pubkey is quite long and we dont want to make the query url too long
        for pubkey_batch in as_chunks(public_keys, self.batch_size):
            data = {}
            # get beacon data for public keys
            beacon_data = (await bacon.get_validators_async("head", ids=pubkey_batch))["data"]
            # update data dict with results
            for d in beacon_data:
                data[d["validator"]["pubkey"]] = int(d["index"])
        
            log.debug(f"Updating {len(data)} minipools with static beacon data")
            # update minipools in db
            bulk = [
                UpdateMany(
                    {"pubkey": a},
                    {"$set": {"validator_index": d}}
                ) for a, d in data.items()
            ]
            await self.db.minipools.bulk_write(bulk, ordered=False)
            
        log.debug("Minipools updated with static beacon data")

    @timerun
    async def update_dynamic_minipool_beacon_metadata(self):
        # basically same ordeal as above, but we use the validator index to get the data to improve performance
        # get all validator indexes from db
        validator_indexes = await self.db.minipools.distinct("validator_index", {"beacon.status": {"$ne": "withdrawal_done"}})
        # remove None values
        validator_indexes = [i for i in validator_indexes if i is not None]
        for index_batch in as_chunks(validator_indexes, self.batch_size):
            data = {}
            # get beacon data for public keys
            beacon_data = (await bacon.get_validators_async("head", ids=index_batch))["data"]
            # update data dict with results
            for d in beacon_data:
                data[int(d["index"])] = {
                    "beacon": {
                        "status"                      : d["status"],
                        "balance"                     : solidity.to_float(d["balance"], 9),
                        "effective_balance"           : solidity.to_float(d["validator"]["effective_balance"], 9),
                        "slashed"                     : d["validator"]["slashed"],
                        "activation_eligibility_epoch": int(d["validator"]["activation_eligibility_epoch"]) if int(
                            d["validator"]["activation_eligibility_epoch"]) < 2 ** 32 else None,
                        "activation_epoch"            : int(d["validator"]["activation_epoch"]) if int(
                            d["validator"]["activation_epoch"]) < 2 ** 32 else None,
                        "exit_epoch"                  : int(d["validator"]["exit_epoch"]) if int(
                            d["validator"]["exit_epoch"]) < 2 ** 32 else None,
                        "withdrawable_epoch"          : int(d["validator"]["withdrawable_epoch"]) if int(
                            d["validator"]["withdrawable_epoch"]) < 2 ** 32 else None,
                    }}
                
            log.debug(f"Updating {len(data)} minipools with dynamic beacon data")
            # update minipools in db
            bulk = [
                UpdateMany(
                    {"validator_index": a},
                    {"$set": d}
                ) for a, d in data.items()
            ]
            await self.db.minipools.bulk_write(bulk, ordered=False)
                
        log.debug("Minipools updated with dynamic beacon data")

    async def check_indexes(self):
        log.debug("checking indexes")
        await self.db.minipools.create_index("address")
        await self.db.minipools.create_index("pubkey")
        await self.db.minipools.create_index("validator_index")
        await self.db.node_operators.create_index("address")
        log.debug("indexes checked")

    @timerun_async
    async def add_untracked_node_operators(self):
        # rocketNodeManager.getNodeCount(i) returns the address of the node at index i
        nm = rp.get_contract_by_name("rocketNodeManager")
        latest_rp = rp.call("rocketNodeManager.getNodeCount") - 1
        # get latest _id in node_operators collection
        latest_db = 0
        if res := await self.db.node_operators.find_one(sort=[("_id", pymongo.DESCENDING)]):
            latest_db = res["_id"]
        data = {}
        # return early if we're up to date
        if latest_db == latest_rp:
            log.debug("No new nodes")
            return
        # batch into self.batch_size nodes at a time, between latest_id and latest_rp
        for index_batch in as_chunks(range(latest_db + 1, latest_rp + 1), self.batch_size):
            data |= await rp.multicall2([
                Call(nm.address, [rp.seth_sig(nm.abi, "getNodeAt"), i], [(i, None)])
                for i in index_batch
            ])
        log.debug(f"Inserting {len(data)} new nodes into db")
        await self.db.node_operators.insert_many([
            {"_id": i, "address": a}
            for i, a in data.items()
        ])
        log.debug("New nodes inserted")

    @timerun_async
    async def add_static_data_to_node_operators(self):
        df = rp.get_contract_by_name("rocketNodeDistributorFactory")
        mf = rp.get_contract_by_name("rocketMegapoolFactory")
        lambs = [
            lambda a: (df.address, [rp.seth_sig(df.abi, "getProxyAddress"), a], [((a, "fee_distributor_address"), None)]),
            lambda a: (mf.address, [rp.seth_sig(mf.abi, "getExpectedAddress"), a], [((a, "megapool_address"), None)]),
        ]
        # get all minipool addresses from db that do not have a node operator assigned
        node_addresses = await self.db.node_operators.distinct(
            "address", 
            {"$or": [
                {"fee_distributor_address": {"$exists": False}}, 
                {"megapool_address": {"$exists": False}
            }]}
        )
        # get node operator addresses from rp
        # return early if no minipools need to be updated
        if not node_addresses:
            log.debug("No node operators need to be updated with static data")
            return
        
        for node_batch in as_chunks(node_addresses, self.batch_size // len(lambs)):
            data = {}
            res = await rp.multicall2(
                [Call(*lamb(a)) for a in node_batch for lamb in lambs], 
                require_success=False
            )
            # update data dict with results
            for (address, variable_name), value in res.items():
                if address not in data:
                    data[address] = {}
                data[address][variable_name] = value
            log.debug(f"Updating {len(data)} node operators with static data")
            # update minipools in db
            bulk = [
                UpdateOne(
                    {"address": a},
                    {"$set": d},
                ) for a, d in data.items()
            ]
            await self.db.node_operators.bulk_write(bulk, ordered=False)
            
        log.debug("Node operators updated with static data")

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
            lambda n: (nm.address, [rp.seth_sig(nm.abi, "getFeeDistributorInitialised"), n["address"]],
                       [((n["address"], "fee_distributor_initialized"), None)]),
            lambda n: (nm.address, [rp.seth_sig(mf.abi, "getMegapoolDeployed"), n["address"]],
                       [((n["address"], "megapool_deployed"), is_true)]),
            lambda n: (nm.address, [rp.seth_sig(nm.abi, "getSmoothingPoolRegistrationState"), n["address"]],
                       [((n["address"], "smoothing_pool_registration"), None)]),
            lambda n: (nm.address, [rp.seth_sig(nm.abi, "getAverageNodeFee"), n["address"]],
                       [((n["address"], "average_node_fee"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeStakedRPL"), n["address"]],
                       [((n["address"], "rpl_stake"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeLegacyStakedRPL"), n["address"]],
                       [((n["address"], "legacy_rpl_stake"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeMegapoolStakedRPL"), n["address"]],
                       [((n["address"], "megapool_rpl_stake"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeLockedRPL"), n["address"]],
                       [((n["address"], "locked_rpl"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeUnstakingRPL"), n["address"]],
                       [((n["address"], "unstaking_rpl"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeRPLStakedTime"), n["address"]],
                       [((n["address"], "last_rpl_stake_time"), None)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeLastUnstakeTime"), n["address"]],
                       [((n["address"], "last_rpl_unstake_time"), None)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeETHCollateralisationRatio"), n["address"]],
                       [((n["address"], "effective_node_share"), safe_inv)]),
            lambda n: (mc.address, [rp.seth_sig(mc.abi, "getEthBalance"), n["fee_distributor_address"]],
                       [((n["address"], "fee_distributor_eth_balance"), safe_to_float)]),
            lambda n: (mc.address, [rp.seth_sig(mc.abi, "getEthBalance"), n["megapool_address"]],
                       [((n["address"], "megapool_eth_balance"), safe_to_float)]),
            lambda n: (mm.address, [rp.seth_sig(mm.abi, "getNodeStakingMinipoolCount"), n["address"]],
                       [((n["address"], "staking_minipool_count"), None)]),
            lambda n: (nd.address, [rp.seth_sig(nd.abi, "getNodeDepositCredit"), n["address"]],
                          [((n["address"], "node_credit"), safe_to_float)]),
            lambda n: (nd.address, [rp.seth_sig(nd.abi, "getNodeEthBalance"), n["address"]],
                          [((n["address"], "node_eth_balance"), safe_to_float)])
        ]
        # get all node operators from db, but we only care about the address and the fee_distributor_address
        nodes = await self.db.node_operators.find({}, {"address": 1, "fee_distributor_address": 1, "megapool_address": 1}).to_list()
        for node_batch in as_chunks(nodes, self.batch_size // len(lambs)):
            data = {}
            res = await rp.multicall2(
                [Call(*lamb(n)) for n in node_batch for lamb in lambs], 
                require_success=False
            )
            # update data dict with results
            for (address, variable_name), value in res.items():
                if address not in data:
                    data[address] = {}
                data[address][variable_name] = value
            log.debug(f"Updating {len(res)} node operator attributes in db")
            # update minipools in db
            bulk = [
                UpdateOne(
                    {"address": a},
                    {"$set": d}
                ) for a, d in data.items()
            ]
            await self.db.node_operators.bulk_write(bulk, ordered=False)
            
        log.debug("Node operators updated with metadata")


async def setup(self):
    await self.add_cog(DBUpkeepTask(self))
