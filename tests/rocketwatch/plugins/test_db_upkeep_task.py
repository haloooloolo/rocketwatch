from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase

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
        assert (
            _derive_validator_status(
                base._replace(staked=False, exiting=False, in_queue=True)
            )
            == "in_queue"
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
