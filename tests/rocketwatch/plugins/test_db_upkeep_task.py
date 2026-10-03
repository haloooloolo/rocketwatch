from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.db_upkeep_task import db_upkeep_task as dut
from rocketwatch.plugins.db_upkeep_task.db_upkeep_task import (
    DBUpkeepTask,
    _derive_validator_status,
    _parse_epoch,
    _unpack_validator_info,
    _unpack_validator_info_dynamic,
    safe_inv,
    safe_state_to_str,
    safe_to_float,
    safe_to_hex,
)
from rocketwatch.utils.rocketpool import ValidatorInfo
from tests.lib.beacon_script import ScriptedBeacon, make_validator_record
from tests.lib.discord_harness import make_bot
from tests.lib.scripted_rocketpool import ScriptedRocketPool

# A staking ValidatorInfo tuple (on-chain field order), reused across megapool
# validator tests. Milliether bond/deposit, gwei exit balance.
_STAKING_INFO = (
    1700,  # last_assignment_time
    0,  # last_requested_value
    4_000,  # last_requested_bond (milliether → 4.0 ETH)
    8_000,  # deposit_value (milliether → 8.0 ETH)
    True,  # staked
    False,  # exited
    False,  # in_queue
    False,  # in_prestake
    True,  # express_used
    False,  # dissolved
    False,  # exiting
    False,  # locked
    32_000_000_000,  # exit_balance (gwei → 32.0 ETH)
    0,  # locked_time
)


def _make_cog(bot: Any) -> DBUpkeepTask:
    # Sidestep __init__: it calls cronitor + schedules a real loop coroutine.
    # Both are irrelevant to the per-method tests below.
    cog = DBUpkeepTask.__new__(DBUpkeepTask)
    cog.bot = bot
    cog.batch_size = 50
    return cog


class TestPureHelpers:
    def test_safe_to_float_handles_garbage(self) -> None:
        assert safe_to_float(10**18) == pytest.approx(1.0)
        assert safe_to_float("not a number") is None  # type: ignore[arg-type]

    def test_safe_inv_zero(self) -> None:
        assert safe_inv(0) is None
        assert safe_inv(10**18) == pytest.approx(1.0)

    def test_safe_to_hex_empty_returns_none(self) -> None:
        assert safe_to_hex(b"") is None
        assert safe_to_hex(b"\xab\xcd") == "0xabcd"

    def test_safe_state_to_str(self) -> None:
        assert safe_state_to_str(2) == "staking"
        # Unknown states pass through as their stringified int.
        assert safe_state_to_str(99) == "99"

    def test_parse_epoch_far_future_is_none(self) -> None:
        assert _parse_epoch(2**40) is None
        assert _parse_epoch(123) == 123

    def test_derive_validator_status_priority(self) -> None:
        # Order in _derive_validator_status: dissolved > exited > in_queue >
        # prestake > locked > exiting > staked. Verify a couple of boundary cases.
        base = ValidatorInfo(
            last_assignment_time=0,
            last_requested_value=0,
            last_requested_bond=0,
            deposit_value=0,
            staked=True,
            exited=False,
            in_queue=False,
            in_prestake=False,
            express_used=False,
            dissolved=False,
            exiting=True,
            locked=False,
            exit_balance=0,
            locked_time=0,
        )
        assert _derive_validator_status(base) == "exiting"
        # dissolved and exited outrank everything else, even with staked/exiting set.
        assert _derive_validator_status(base._replace(dissolved=True)) == "dissolved"
        assert _derive_validator_status(base._replace(exited=True)) == "exited"
        assert (
            _derive_validator_status(
                base._replace(staked=False, exiting=False, in_queue=True)
            )
            == "in_queue"
        )
        assert (
            _derive_validator_status(
                base._replace(staked=False, exiting=False, in_prestake=True)
            )
            == "prestaked"
        )
        assert (
            _derive_validator_status(
                base._replace(staked=False, exiting=False, locked=True)
            )
            == "locked"
        )
        assert (
            _derive_validator_status(base._replace(staked=False, exiting=False))
            == "unknown"
        )

    def test_unpack_validator_info_scales_units(self) -> None:
        info = ValidatorInfo(
            last_assignment_time=1700,
            last_requested_value=0,
            # On-chain unit for these is milliether.
            last_requested_bond=4_000,
            deposit_value=8_000,
            staked=True,
            exited=False,
            in_queue=False,
            in_prestake=False,
            express_used=True,
            dissolved=False,
            exiting=False,
            locked=False,
            # gwei → ETH scaling.
            exit_balance=32_000_000_000,
            locked_time=0,
        )
        assert _unpack_validator_info(info) == {
            "status": "staking",
            "express_used": True,
            "assignment_time": 1700,
            "requested_bond": 4.0,
            "deposit_value": 8.0,
            "exit_balance": pytest.approx(32.0),
        }

    def test_unpack_validator_info_dynamic_drops_express_used(self) -> None:
        info = ValidatorInfo(
            last_assignment_time=42,
            last_requested_value=0,
            last_requested_bond=1_000,
            deposit_value=2_000,
            staked=True,
            exited=False,
            in_queue=False,
            in_prestake=False,
            express_used=True,
            dissolved=False,
            exiting=False,
            locked=False,
            exit_balance=0,
            locked_time=0,
        )
        out = _unpack_validator_info_dynamic(info)
        assert "express_used" not in out
        assert out["assignment_time"] == 42


class TestUpdateMinipoolBeaconData:
    async def test_writes_decoded_beacon_state(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        await mongo_db.minipools.insert_many(
            [
                {"_id": 1, "pubkey": "0xaa", "beacon": {"status": "active_ongoing"}},
                {"_id": 2, "pubkey": "0xbb", "beacon": {"status": "active_ongoing"}},
                # withdrawal_done is excluded by the query — should not be touched.
                {"_id": 3, "pubkey": "0xcc", "beacon": {"status": "withdrawal_done"}},
                # null pubkey is filtered out before the beacon call.
                {"_id": 4, "pubkey": None},
            ]
        )
        scripted_bacon.register_validators(
            [
                make_validator_record(
                    pubkey="0xaa",
                    index=1001,
                    status="active_ongoing",
                    balance_gwei=32_500_000_000,
                    effective_balance_gwei=32_000_000_000,
                    activation_epoch=10,
                    exit_epoch=2**40,  # far-future → stored as None
                ),
                make_validator_record(
                    pubkey="0xbb",
                    index=1002,
                    status="active_exiting",
                    slashed=True,
                    exit_epoch=50,
                ),
            ]
        )

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.update_dynamic_minipool_beacon_data()

        mp1 = await mongo_db.minipools.find_one({"_id": 1})
        assert mp1 is not None
        assert mp1["validator_index"] == 1001
        assert mp1["beacon"]["status"] == "active_ongoing"
        assert mp1["beacon"]["balance"] == pytest.approx(32.5)
        assert mp1["beacon"]["effective_balance"] == pytest.approx(32.0)
        assert mp1["beacon"]["slashed"] is False
        assert mp1["beacon"]["activation_epoch"] == 10
        assert mp1["beacon"]["exit_epoch"] is None  # far-future sentinel

        mp2 = await mongo_db.minipools.find_one({"_id": 2})
        assert mp2 is not None
        assert mp2["beacon"]["slashed"] is True
        assert mp2["beacon"]["exit_epoch"] == 50

        # Untouched: still has its original beacon, no validator_index.
        mp3 = await mongo_db.minipools.find_one({"_id": 3})
        assert mp3 is not None
        assert "validator_index" not in mp3

        mp4 = await mongo_db.minipools.find_one({"_id": 4})
        assert mp4 is not None
        assert "validator_index" not in mp4

    async def test_no_eligible_minipools_is_noop(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        # All minipools are filtered out — bacon must not be called.
        await mongo_db.minipools.insert_one(
            {"_id": 1, "pubkey": "0xaa", "beacon": {"status": "withdrawal_done"}}
        )

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.update_dynamic_minipool_beacon_data()
        # If bacon had been called, the empty script would raise KeyError.


class TestUpdateMegapoolValidatorBeaconData:
    async def test_writes_decoded_beacon_state(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        await mongo_db.megapool_validators.insert_many(
            [
                {
                    "_id": 1,
                    "megapool": "0xMP",
                    "validator_id": 0,
                    "pubkey": "0xa1",
                },
                {
                    "_id": 2,
                    "megapool": "0xMP",
                    "validator_id": 1,
                    "pubkey": "0xa2",
                    "beacon": {"status": "withdrawal_done"},  # filtered out
                },
            ]
        )
        scripted_bacon.register_validator(
            make_validator_record(
                pubkey="0xa1",
                index=42,
                status="pending_queued",
                balance_gwei=1_000_000_000,
            )
        )

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.update_dynamic_megapool_validator_beacon_data()

        v1 = await mongo_db.megapool_validators.find_one({"_id": 1})
        assert v1 is not None
        assert v1["validator_index"] == 42
        assert v1["beacon"]["status"] == "pending_queued"

        v2 = await mongo_db.megapool_validators.find_one({"_id": 2})
        assert v2 is not None
        assert "validator_index" not in v2

    async def test_empty_db_is_noop(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.update_dynamic_megapool_validator_beacon_data()


class TestCheckIndexes:
    async def test_creates_unique_compound_index(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.check_indexes()
        # idempotent — running twice must not raise.
        await cog.check_indexes()

        mv_idx = await mongo_db.megapool_validators.index_information()
        compound = [
            v
            for v in mv_idx.values()
            if v["key"] == [("megapool", 1), ("validator_id", 1)]
        ]
        assert compound and compound[0].get("unique")

        mp_idx = await mongo_db.minipools.index_information()
        assert any(v["key"] == [("pubkey", 1)] for v in mp_idx.values())


class TestAddUntrackedNodeOperators:
    async def test_appends_new_nodes(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
    ) -> None:
        await mongo_db.node_operators.insert_one({"_id": 0, "address": "0xN0"})
        scripted_rp.set_call("rocketNodeManager.getNodeCount", 3)  # latest index 2
        scripted_rp.set_call("rocketNodeManager.getNodeAt", lambda i: f"0xN{i}")

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.add_untracked_node_operators()

        ids = {d["_id"]: d["address"] async for d in mongo_db.node_operators.find()}
        assert ids == {0: "0xN0", 1: "0xN1", 2: "0xN2"}

    async def test_no_new_nodes_is_noop(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
    ) -> None:
        await mongo_db.node_operators.insert_one({"_id": 5, "address": "0xN5"})
        scripted_rp.set_call("rocketNodeManager.getNodeCount", 3)

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.add_untracked_node_operators()

        assert await mongo_db.node_operators.count_documents({}) == 1


class TestAddStaticNodeOperatorData:
    async def test_fills_derived_addresses(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
    ) -> None:
        await mongo_db.node_operators.insert_one({"_id": 1, "address": "0xN1"})
        scripted_rp.set_call(
            "rocketNodeDistributorFactory.getProxyAddress", lambda a: f"{a}_fd"
        )
        scripted_rp.set_call(
            "rocketMegapoolFactory.getExpectedAddress", lambda a: f"{a}_mp"
        )

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.add_static_node_operator_data()

        doc = await mongo_db.node_operators.find_one({"_id": 1})
        assert doc is not None
        assert doc["fee_distributor"]["address"] == "0xN1_fd"
        assert doc["megapool"]["address"] == "0xN1_mp"


class TestUpdateDynamicNodeOperatorData:
    async def test_writes_transformed_fields(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
    ) -> None:
        await mongo_db.node_operators.insert_one(
            {
                "_id": 1,
                "address": "0xN1",
                "fee_distributor": {"address": "0xFD"},
                "megapool": {"address": "0xMP"},
            }
        )
        # Every call in the spec must resolve or multicall raises; default the
        # lot to 0, then override the ones whose transforms we assert.
        paths = [
            "rocketNodeManager.getNodeWithdrawalAddress",
            "rocketNodeManager.getNodeTimezoneLocation",
            "rocketNodeManager.getSmoothingPoolRegistrationState",
            "rocketNodeManager.getAverageNodeFee",
            "rocketNodeStaking.getNodeETHCollateralisationRatio",
            "rocketMinipoolManager.getNodeStakingMinipoolCount",
            "rocketNodeDeposit.getNodeDepositCredit",
            "rocketNodeDeposit.getNodeEthBalance",
            "rocketNodeManager.getFeeDistributorInitialised",
            "multicall3.getEthBalance",
            "rocketMegapoolFactory.getMegapoolDeployed",
            "rocketNodeStaking.getNodeStakedRPL",
            "rocketNodeStaking.getNodeLegacyStakedRPL",
            "rocketNodeStaking.getNodeMegapoolStakedRPL",
            "rocketNodeStaking.getNodeLockedRPL",
            "rocketNodeStaking.getNodeUnstakingRPL",
            "rocketNodeStaking.getNodeRPLStakedTime",
            "rocketNodeStaking.getNodeLastUnstakeTime",
        ]
        for p in paths:
            scripted_rp.set_call(p, 0)
        scripted_rp.set_call("rocketNodeManager.getNodeWithdrawalAddress", "0xWD")
        scripted_rp.set_call("rocketNodeManager.getAverageNodeFee", 10**18)  # → 1.0
        scripted_rp.set_call(
            "rocketNodeStaking.getNodeETHCollateralisationRatio",
            10**18,  # safe_inv → 1.0
        )
        scripted_rp.set_call("rocketMegapoolFactory.getMegapoolDeployed", True)
        scripted_rp.set_call("rocketNodeStaking.getNodeStakedRPL", 5 * 10**18)  # → 5.0

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.update_dynamic_node_operator_data()

        doc = await mongo_db.node_operators.find_one({"_id": 1})
        assert doc is not None
        assert doc["withdrawal_address"] == "0xWD"
        assert doc["average_node_fee"] == pytest.approx(1.0)
        assert doc["effective_node_share"] == pytest.approx(1.0)
        assert doc["megapool"]["deployed"] is True
        assert doc["rpl"]["total_stake"] == pytest.approx(5.0)


class TestAddUntrackedMinipools:
    async def test_appends_new_minipools(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
    ) -> None:
        await mongo_db.minipools.insert_one({"_id": 0, "address": "0xMP0"})
        scripted_rp.set_call("rocketMinipoolManager.getMinipoolCount", 3)
        scripted_rp.set_call(
            "rocketMinipoolManager.getMinipoolAt", lambda i: f"0xMP{i}"
        )

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.add_untracked_minipools()

        ids = {d["_id"]: d["address"] async for d in mongo_db.minipools.find()}
        assert ids == {0: "0xMP0", 1: "0xMP1", 2: "0xMP2"}


class TestAddUntrackedMegapoolValidators:
    async def test_inserts_per_validator_docs(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
    ) -> None:
        await mongo_db.node_operators.insert_one(
            {
                "_id": 1,
                "address": "0xN",
                "megapool": {
                    "address": "0xMP",
                    "deployed": True,
                    "validator_count": 2,
                },
            }
        )
        scripted_rp.set_call(
            "rocketMegapoolDelegate.getValidatorPubkey", lambda vid: bytes([vid + 1])
        )
        scripted_rp.set_call(
            "rocketMegapoolDelegate.getValidatorInfo", lambda vid: _STAKING_INFO
        )

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.add_untracked_megapool_validators()

        docs = {
            d["validator_id"]: d
            async for d in mongo_db.megapool_validators.find({"megapool": "0xMP"})
        }
        assert set(docs) == {0, 1}
        assert docs[0]["pubkey"] == "0x01"
        assert docs[1]["pubkey"] == "0x02"
        assert docs[0]["node_operator"] == "0xN"
        assert docs[0]["status"] == "staking"
        assert docs[0]["requested_bond"] == pytest.approx(4.0)

    async def test_skips_when_db_already_complete(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
    ) -> None:
        await mongo_db.node_operators.insert_one(
            {
                "_id": 1,
                "address": "0xN",
                "megapool": {"address": "0xMP", "deployed": True, "validator_count": 1},
            }
        )
        await mongo_db.megapool_validators.insert_one(
            {"megapool": "0xMP", "validator_id": 0, "pubkey": "0xaa"}
        )

        cog = _make_cog(make_bot(db=mongo_db))
        # No calls scripted: if it tried to fetch, multicall would KeyError.
        await cog.add_untracked_megapool_validators()
        assert await mongo_db.megapool_validators.count_documents({}) == 1


class TestUpdateDynamicMegapoolData:
    async def test_writes_megapool_fields(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
    ) -> None:
        await mongo_db.node_operators.insert_one(
            {
                "_id": 1,
                "address": "0xN",
                "megapool": {"address": "0xMP", "deployed": True},
            }
        )
        for method in (
            "getValidatorCount",
            "getActiveValidatorCount",
            "getExitingValidatorCount",
            "getLockedValidatorCount",
            "getNodeBond",
            "getUserCapital",
            "getDebt",
            "getRefundValue",
            "getPendingRewards",
            "getLastDistributionTime",
            "getDelegate",
            "getEffectiveDelegate",
            "getUseLatestDelegate",
        ):
            scripted_rp.set_call(f"0xMP.{method}", 0)
        scripted_rp.set_call("0xMP.getValidatorCount", 5)
        scripted_rp.set_call("0xMP.getNodeBond", 4 * 10**18)  # → 4.0
        scripted_rp.set_call("0xMP.getDelegate", "0xDEL")

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.update_dynamic_megapool_data()

        doc = await mongo_db.node_operators.find_one({"_id": 1})
        assert doc is not None
        assert doc["megapool"]["validator_count"] == 5
        assert doc["megapool"]["node_bond"] == pytest.approx(4.0)
        assert doc["megapool"]["delegate"] == "0xDEL"

    async def test_skips_undeployed_megapools(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
    ) -> None:
        await mongo_db.node_operators.insert_one(
            {
                "_id": 1,
                "address": "0xN",
                "megapool": {"address": "0xMP", "deployed": False},
            }
        )
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.update_dynamic_megapool_data()  # no calls scripted → must not fetch
        doc = await mongo_db.node_operators.find_one({"_id": 1})
        assert doc is not None
        assert "validator_count" not in doc["megapool"]


class TestAddStaticMinipoolData:
    async def test_fills_node_and_pubkey(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
    ) -> None:
        await mongo_db.minipools.insert_one({"_id": 1, "address": "0xMP1"})
        scripted_rp.set_call("0xMP1.getNodeAddress", "0xNODE")
        scripted_rp.set_call(
            "rocketMinipoolManager.getMinipoolPubkey", lambda a: b"\xaa\xbb"
        )

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.add_static_minipool_data()

        doc = await mongo_db.minipools.find_one({"_id": 1})
        assert doc is not None
        assert doc["node_operator"] == "0xNODE"
        assert doc["pubkey"] == "0xaabb"


class TestUpdateDynamicMinipoolData:
    async def test_writes_transformed_fields(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
    ) -> None:
        await mongo_db.minipools.insert_one({"_id": 1, "address": "0xMP1"})
        for method in (
            "getStatus",
            "getStatusTime",
            "getVacant",
            "getFinalised",
            "getNodeDepositBalance",
            "getNodeRefundBalance",
            "getPreMigrationBalance",
            "getNodeFee",
            "getDelegate",
            "getPreviousDelegate",
            "getEffectiveDelegate",
            "getUseLatestDelegate",
            "getUserDistributed",
        ):
            scripted_rp.set_call(f"0xMP1.{method}", 0)
        scripted_rp.set_call("0xMP1.getStatus", 2)  # → "staking"
        scripted_rp.set_call("0xMP1.getNodeDepositBalance", 8 * 10**18)  # → 8.0
        scripted_rp.set_call("0xMP1.getDelegate", "0xDEL")
        scripted_rp.set_call("0xMP1.getFinalised", False)
        scripted_rp.set_call("multicall3.getEthBalance", 32 * 10**18)  # → 32.0

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.update_dynamic_minipool_data()

        doc = await mongo_db.minipools.find_one({"_id": 1})
        assert doc is not None
        assert doc["status"] == "staking"
        assert doc["node_deposit_balance"] == pytest.approx(8.0)
        assert doc["execution_balance"] == pytest.approx(32.0)
        assert doc["finalized"] is False
        assert doc["delegate"] == "0xDEL"


class TestUpdateDynamicMegapoolValidatorData:
    async def test_writes_dynamic_validator_fields(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
    ) -> None:
        await mongo_db.megapool_validators.insert_one(
            {"_id": 1, "megapool": "0xMP", "validator_id": 0, "status": "staking"}
        )
        scripted_rp.set_call("0xMP.getValidatorInfo", lambda vid: _STAKING_INFO)

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.update_dynamic_megapool_validator_data()

        doc = await mongo_db.megapool_validators.find_one({"_id": 1})
        assert doc is not None
        assert doc["status"] == "staking"
        assert doc["requested_bond"] == pytest.approx(4.0)
        assert doc["deposit_value"] == pytest.approx(8.0)
        assert doc["exit_balance"] == pytest.approx(32.0)
        # _unpack_validator_info_dynamic drops express_used.
        assert "express_used" not in doc

    async def test_skips_exited_validators(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
    ) -> None:
        await mongo_db.megapool_validators.insert_one(
            {"_id": 1, "megapool": "0xMP", "validator_id": 0, "status": "exited"}
        )
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.update_dynamic_megapool_validator_data()  # filtered out → no fetch
        doc = await mongo_db.megapool_validators.find_one({"_id": 1})
        assert doc is not None
        assert doc["status"] == "exited"


def _log(
    event: str, *, block: int, tx: str, idx: int, args: dict[str, Any]
) -> dict[str, Any]:
    return {
        "event": event,
        "blockNumber": block,
        "transactionIndex": 0,
        "logIndex": idx,
        "transactionHash": tx,
        "args": args,
    }


class TestAddStaticMinipoolDepositData:
    async def test_pairs_deposit_and_creation_events(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.minipools.insert_one(
            {"_id": 1, "address": "0xmp1", "status": "initialised", "status_time": 1700}
        )
        # Same tx: DepositReceived (logIndex 0) then MinipoolCreated (logIndex 1).
        # MinipoolCreated's address is upper-cased to prove the match lowercases.
        deposit = _log(
            "DepositReceived", block=100, tx="0xtx", idx=0, args={"amount": 16 * 10**18}
        )
        creation = _log(
            "MinipoolCreated", block=100, tx="0xtx", idx=1, args={"minipool": "0xMP1"}
        )

        async def fake_get_logs(event: Any, *_: Any, **__: Any) -> list[dict[str, Any]]:
            return {"DepositReceived": [deposit], "MinipoolCreated": [creation]}.get(
                event.event_name, []
            )

        monkeypatch.setattr(dut, "get_logs", fake_get_logs)
        monkeypatch.setattr(dut, "ts_to_block", AsyncMock(return_value=100))

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.add_static_minipool_deposit_data()

        doc = await mongo_db.minipools.find_one({"_id": 1})
        assert doc is not None
        assert doc["deposit_amount"] == pytest.approx(16.0)

    async def test_no_eligible_minipools_is_noop(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
    ) -> None:
        # Already has a deposit_amount → excluded by the query.
        await mongo_db.minipools.insert_one(
            {
                "_id": 1,
                "address": "0xmp1",
                "status": "initialised",
                "deposit_amount": 1.0,
            }
        )
        cog = _make_cog(make_bot(db=mongo_db))
        await (
            cog.add_static_minipool_deposit_data()
        )  # no get_logs patched → must not call


class TestAddStaticMegapoolDepositData:
    async def test_writes_deposit_time(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.megapool_validators.insert_one(
            {"_id": 1, "megapool": "0xMP", "validator_id": 0}
        )
        scripted_w3.eth.get_block_number = AsyncMock(return_value=25_000_000)
        event = _log(
            "FundsRequested",
            block=24_500_000,
            tx="0xtx",
            idx=0,
            args={"validatorId": 0, "time": 1234},
        )

        async def fake_get_logs(*_: Any, **__: Any) -> list[dict[str, Any]]:
            return [event]

        monkeypatch.setattr(dut, "get_logs", fake_get_logs)
        monkeypatch.setattr(dut, "ts_to_block", AsyncMock(return_value=24_400_000))

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.add_static_megapool_deposit_data()

        doc = await mongo_db.megapool_validators.find_one({"_id": 1})
        assert doc is not None
        assert doc["deposit_time"] == 1234

    async def test_derives_from_block_for_later_validators(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_w3: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # validator_id 1 has no deposit_time; its predecessor (0) does, so the
        # search window starts from ts_to_block(prev deposit_time).
        await mongo_db.megapool_validators.insert_many(
            [
                {"_id": 0, "megapool": "0xMP", "validator_id": 0, "deposit_time": 1000},
                {"_id": 1, "megapool": "0xMP", "validator_id": 1},
            ]
        )
        scripted_w3.eth.get_block_number = AsyncMock(return_value=25_000_000)
        ts_to_block = AsyncMock(return_value=24_600_000)
        monkeypatch.setattr(dut, "ts_to_block", ts_to_block)

        async def fake_get_logs(*_: Any, **__: Any) -> list[dict[str, Any]]:
            return [
                _log(
                    "FundsRequested",
                    block=24_700_000,
                    tx="0xtx",
                    idx=0,
                    args={"validatorId": 1, "time": 5678},
                )
            ]

        monkeypatch.setattr(dut, "get_logs", fake_get_logs)

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.add_static_megapool_deposit_data()

        doc = await mongo_db.megapool_validators.find_one({"_id": 1})
        assert doc is not None
        assert doc["deposit_time"] == 5678
        ts_to_block.assert_awaited_once_with(1000)
