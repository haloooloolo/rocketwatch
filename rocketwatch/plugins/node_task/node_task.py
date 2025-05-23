import logging
import time

import pymongo
from multicall import Call
from cronitor import Monitor
from pymongo import UpdateOne, UpdateMany

from discord.ext import tasks, commands

from rocketwatch import RocketWatch
from utils import solidity
from utils.cfg import cfg
from utils.block_time import ts_to_block
from utils.rocketpool import rp
from utils.shared_w3 import bacon
from utils.time_debug import timerun, timerun_async
from utils.event_logs import get_logs


log = logging.getLogger("node_task")
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


class NodeTask(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.db = pymongo.MongoClient(cfg["mongodb.uri"]).rocketwatch
        self.monitor = Monitor("node-task", api_key=cfg["other.secrets.cronitor"])
        self.batch_size = 1000
        self.loop.start()
            
    def cog_unload(self):
        self.loop.cancel()
        
    @tasks.loop(seconds=solidity.BEACON_EPOCH_LENGTH)
    async def loop(self):
        p_id = time.time() 
        self.monitor.ping(state="run", series=p_id)
        try:
            log.debug("starting node task")
            self.check_indexes()
            await self.add_untracked_minipools()
            await self.add_static_data_to_minipools()
            await self.update_dynamic_minipool_metadata()
            self.add_static_deposit_data_to_minipools()
            self.add_static_beacon_data_to_minipools()
            self.update_dynamic_minipool_beacon_metadata()
            await self.add_untracked_node_operators()
            await self.add_static_data_to_node_operators()
            await self.update_dynamic_node_operator_metadata()
            log.debug("node task finished")
            self.monitor.ping(state="complete", series=p_id)
        except Exception as err:
            await self.bot.report_error(err)
            self.monitor.ping(state="fail", series=p_id)
        
    @loop.before_loop
    async def on_ready(self):
        await self.bot.wait_until_ready()

    @timerun_async
    async def add_untracked_minipools(self):
        # rocketMinipoolManager.getMinipoolAt(i) returns the address of the minipool at index i
        mm = rp.get_contract_by_name("rocketMinipoolManager")
        latest_rp = rp.call("rocketMinipoolManager.getMinipoolCount") - 1
        # get latest _id in minipools_new collection
        latest_db = 0
        if res := self.db.minipools_new.find_one(sort=[("_id", pymongo.DESCENDING)]):
            latest_db = res["_id"]
        data = {}
        # return early if we're up to date
        if latest_db == latest_rp:
            log.debug("No new minipools")
            return
        log.debug(f"Latest minipool in db: {latest_db}, latest minipool in rp: {latest_rp}")
        # batch into self.batch_size minipools at a time, between latest_id and minipool_count
        for i in range(latest_db + 1, latest_rp + 1, self.batch_size):
            i_end = min(i + self.batch_size, latest_rp + 1)
            log.debug(f"Getting untracked minipools ({i} to {i_end})")
            data |= await rp.multicall2([
                Call(mm.address, [rp.seth_sig(mm.abi, "getMinipoolAt"), i], [(i, None)])
                for i in range(i, i_end)
            ])
        log.debug(f"Inserting {len(data)} new minipools into db")
        self.db.minipools_new.insert_many([
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
        minipool_addresses = self.db.minipools_new.distinct("address", {"node_operator": {"$exists": False}})
        # get node operator addresses from rp
        # return early if no minipools need to be updated
        if not minipool_addresses:
            log.debug("No minipools need to be updated with static data")
            return
        data = {}
        batch_size = self.batch_size // len(lambs)
        for i in range(0, len(minipool_addresses), batch_size):
            i_end = min(i + batch_size, len(minipool_addresses))
            log.debug(f"Getting minipool static data ({i} to {i_end})")
            res = await rp.multicall2([
                Call(*lamb(a))
                for a in minipool_addresses[i:i_end]
                for lamb in lambs
            ], require_success=False)
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
        self.db.minipools_new.bulk_write(bulk, ordered=False)
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
            lambda a: (mc.address, [rp.seth_sig(mc.abi, "getEthBalance"), a], [((a, "execution_balance"), safe_to_float)])
        ]
        # get all minipool addresses from db
        minipool_addresses = self.db.minipools_new.distinct("address")
        data = {}
        att_count = 0
        batch_size = self.batch_size // len(lambs)
        for i in range(0, len(minipool_addresses), batch_size):
            i_end = min(i + batch_size, len(minipool_addresses))
            log.debug(f"Getting minipool metadata ({i} to {i_end})")
            res = await rp.multicall2([
                Call(*lamb(a))
                for a in minipool_addresses[i:i_end]
                for lamb in lambs
            ], require_success=False)
            # update data dict with results
            for (address, variable_name), value in res.items():
                if address not in data:
                    data[address] = {}
                data[address][variable_name] = value
                att_count += 1
        log.debug(f"Updating {att_count} minipool attributes in db")
        # update minipools in db
        bulk = [
            UpdateOne(
                {"address": a},
                {"$set": d}
            ) for a, d in data.items()
        ]
        self.db.minipools_new.bulk_write(bulk, ordered=False)
        log.debug("Minipools updated with metadata")
        return

    @timerun
    def add_static_deposit_data_to_minipools(self):
        # get all minipool addresses and their status time from db that :
        # - do not have a deposit_amount
        # - are in the initialised state
        # sort by status time
        minipools = list(self.db.minipools_new.find(
            {"deposit_amount": {"$exists": False}, "status": "initialised"},
            {"address": 1, "_id": 0, "status_time": 1}
        ).sort("status_time", pymongo.ASCENDING))
        # return early if no minipools need to be updated
        if not minipools:
            log.debug("No minipools need to be updated with static deposit data")
            return
        nd = rp.get_contract_by_name("rocketNodeDeposit")
        mm = rp.get_contract_by_name("rocketMinipoolManager")
        data = {}
        for i in range(0, len(minipools), self.batch_size):
            i_end = min(i + self.batch_size, len(minipools))
            # turn status time of first and last minipool into blocks
            block_start = ts_to_block(minipools[i]["status_time"]) - 1
            block_end = ts_to_block(minipools[i_end - 1]["status_time"]) + 1
            a = [m["address"] for m in minipools[i:i_end]]
            log.debug(f"Getting minipool deposit data ({i} to {i_end})")
            
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
        if len(data) == 0:
            log.debug("No minipools need to be updated with static deposit data")
            return
        log.debug(f"Updating {len(data)} minipools with static deposit data")
        # update minipools in db
        bulk = [
            UpdateOne(
                {"address": a},
                {"$set": d},
            ) for a, d in data.items()
        ]
        self.db.minipools_new.bulk_write(bulk, ordered=False)
        log.debug("Minipools updated with static deposit data")


    @timerun
    def add_static_beacon_data_to_minipools(self):
        # get all public keys from db where no validator_index is set
        public_keys = self.db.minipools_new.distinct("pubkey", {"validator_index": {"$exists": False}})
        # return early if no minipools need to be updated
        if not public_keys:
            log.debug("No minipools need to be updated with static beacon data")
            return
        # we need to do smaller bulks as the pubkey is qutie long and we dont want to make the query url too long
        data = {}
        # endpoint = bacon.get_validators("head", ids=vali_indexes)["data"]
        for i in range(0, len(public_keys), self.batch_size):
            i_end = min(i + self.batch_size, len(public_keys))
            log.debug(f"Getting beacon data for minipools ({i} to {i_end})")
            # get beacon data for public keys
            beacon_data = bacon.get_validators("head", ids=public_keys[i:i_end])["data"]
            # update data dict with results
            for d in beacon_data:
                data[d["validator"]["pubkey"]] = int(d["index"])
        if not data:
            log.debug("No minipools need to be updated with static beacon data")
            return
        log.debug(f"Updating {len(data)} minipools with static beacon data")
        # update minipools in db
        bulk = [
            UpdateMany(
                {"pubkey": a},
                {"$set": {"validator_index": d}}
            ) for a, d in data.items()
        ]
        self.db.minipools_new.bulk_write(bulk, ordered=False)
        log.debug("Minipools updated with static beacon data")

    @timerun
    def update_dynamic_minipool_beacon_metadata(self):
        # basically same ordeal as above, but we use the validator index to get the data to improve performance
        # get all validator indexes from db
        validator_indexes = self.db.minipools_new.distinct("validator_index")
        # remove None values
        validator_indexes = [i for i in validator_indexes if i is not None]
        data = {}
        # endpoint = bacon.get_validators("head", ids=vali_indexes)["data"]
        for i in range(0, len(validator_indexes), self.batch_size):
            i_end = min(i + self.batch_size, len(validator_indexes))
            log.debug(f"Getting beacon data for minipools ({i} to {i_end})")
            # get beacon data for public keys
            beacon_data = bacon.get_validators("head", ids=validator_indexes[i:i_end])["data"]
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
        self.db.minipools_new.bulk_write(bulk, ordered=False)
        log.debug("Minipools updated with dynamic beacon data")

    def check_indexes(self):
        log.debug("checking indexes")
        self.db.minipools_new.create_index("address")
        self.db.minipools_new.create_index("pubkey")
        self.db.minipools_new.create_index("validator_index")
        self.db.node_operators_new.create_index("address")
        # proposal index creation that is for some reason here
        self.db.proposals.create_index("validator")
        self.db.proposals.create_index("validator")
        self.db.proposals.create_index("slot", unique=True)
        log.debug("indexes checked")

    @timerun_async
    async def add_untracked_node_operators(self):
        # rocketNodeManager.getNodeCount(i) returns the address of the node at index i
        nm = rp.get_contract_by_name("rocketNodeManager")
        latest_rp = rp.call("rocketNodeManager.getNodeCount") - 1
        # get latest _id in node_operators_new collection
        latest_db = 0
        if res := self.db.node_operators_new.find_one(sort=[("_id", pymongo.DESCENDING)]):
            latest_db = res["_id"]
        data = {}
        # return early if we're up to date
        if latest_db == latest_rp:
            log.debug("No new nodes")
            return
        # batch into 10k nodes at a time, between latest_id and latest_rp
        for i in range(latest_db + 1, latest_rp + 1, self.batch_size):
            i_end = min(i + self.batch_size, latest_rp + 1)
            log.debug(f"Getting untracked node ({i} to {i_end})")
            data |= await rp.multicall2([
                Call(nm.address, [rp.seth_sig(nm.abi, "getNodeAt"), i], [(i, None)])
                for i in range(i, i_end)
            ])
        log.debug(f"Inserting {len(data)} new nodes into db")
        self.db.node_operators_new.insert_many([
            {"_id": i, "address": a}
            for i, a in data.items()
        ])
        log.debug("New nodes inserted")

    @timerun_async
    async def add_static_data_to_node_operators(self):
        ndf = rp.get_contract_by_name("rocketNodeDistributorFactory")
        lambs = [
            lambda a: (ndf.address, [rp.seth_sig(ndf.abi, "getProxyAddress"), a], [((a, "fee_distributor_address"), None)]),
        ]
        # get all minipool addresses from db that do not have a node operator assigned
        node_addresses = self.db.node_operators_new.distinct("address", {"fee_distributor_address": {"$exists": False}})
        # get node operator addresses from rp
        # return early if no minipools need to be updated
        if not node_addresses:
            log.debug("No node operators need to be updated with static data")
            return
        data = {}
        batch_size = self.batch_size // len(lambs)
        for i in range(0, len(node_addresses), batch_size):
            i_end = min(i + batch_size, len(node_addresses))
            log.debug(f"Getting node operators static data ({i} to {i_end})")
            res = await rp.multicall2([
                Call(*lamb(a))
                for a in node_addresses[i:i_end]
                for lamb in lambs
            ], require_success=False)
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
        self.db.node_operators_new.bulk_write(bulk, ordered=False)
        log.debug("Node operators updated with static data")

    @timerun_async
    async def update_dynamic_node_operator_metadata(self):
        ndf = rp.get_contract_by_name("rocketNodeDistributorFactory")
        nd = rp.get_contract_by_name("rocketNodeDeposit")
        nm = rp.get_contract_by_name("rocketNodeManager")
        mm = rp.get_contract_by_name("rocketMinipoolManager")
        ns = rp.get_contract_by_name("rocketNodeStaking")
        mc = rp.get_contract_by_name("multicall3")
        lambs = [
            lambda n: (ndf.address, [rp.seth_sig(ndf.abi, "getProxyAddress"), n["address"]],
                       [((n["address"], "fee_distributor_address"), None)]),
            lambda n: (nm.address, [rp.seth_sig(nm.abi, "getNodeWithdrawalAddress"), n["address"]],
                       [((n["address"], "withdrawal_address"), None)]),
            lambda n: (nm.address, [rp.seth_sig(nm.abi, "getNodeTimezoneLocation"), n["address"]],
                       [((n["address"], "timezone_location"), None)]),
            lambda n: (nm.address, [rp.seth_sig(nm.abi, "getFeeDistributorInitialised"), n["address"]],
                       [((n["address"], "fee_distributor_initialised"), None)]),
            lambda n: (
                nm.address, [rp.seth_sig(nm.abi, "getRewardNetwork"), n["address"]],
                [((n["address"], "reward_network"), None)]),
            lambda n: (nm.address, [rp.seth_sig(nm.abi, "getSmoothingPoolRegistrationState"), n["address"]],
                       [((n["address"], "smoothing_pool_registration_state"), None)]),
            lambda n: (nm.address, [rp.seth_sig(nm.abi, "getAverageNodeFee"), n["address"]],
                       [((n["address"], "average_node_fee"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeRPLStake"), n["address"]],
                       [((n["address"], "rpl_stake"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeEffectiveRPLStake"), n["address"]],
                       [((n["address"], "effective_rpl_stake"), safe_to_float)]),
            lambda n: (ns.address, [rp.seth_sig(ns.abi, "getNodeETHCollateralisationRatio"), n["address"]],
                       [((n["address"], "effective_node_share"), safe_inv)]),
            lambda n: (mc.address, [rp.seth_sig(mc.abi, "getEthBalance"), n["fee_distributor_address"]],
                       [((n["address"], "fee_distributor_eth_balance"), safe_to_float)]),
            lambda n: (mm.address, [rp.seth_sig(mm.abi, "getNodeStakingMinipoolCount"), n["address"]],
                       [((n["address"], "staking_minipool_count"), None)]),
            lambda n: (nd.address, [rp.seth_sig(nd.abi, "getNodeDepositCredit"), n["address"]],
                          [((n["address"], "deposit_credit"), safe_to_float)])
        ]
        # get all node operators from db, but we only care about the address and the fee_distributor_address
        nodes = list(self.db.node_operators_new.find({}, {"address": 1, "fee_distributor_address": 1}))
        data = {}
        att_count = 0
        batch_size = self.batch_size // len(lambs)
        for i in range(0, len(nodes), batch_size):
            i_end = min(i + batch_size, len(nodes))
            log.debug(f"Getting node operator metadata ({i} to {i_end})")
            res = await rp.multicall2([
                Call(*lamb(n))
                for n in nodes[i:i_end]
                for lamb in lambs
            ], require_success=False)
            # update data dict with results
            for (address, variable_name), value in res.items():
                if address not in data:
                    data[address] = {}
                data[address][variable_name] = value
                att_count += 1
        log.debug(f"Updating {att_count} node operator attributes in db")
        # update minipools in db
        bulk = [
            UpdateOne(
                {"address": a},
                {"$set": d}
            ) for a, d in data.items()
        ]
        self.db.node_operators_new.bulk_write(bulk, ordered=False)
        log.debug("Node operators updated with metadata")

async def setup(self):
    await self.add_cog(NodeTask(self))
