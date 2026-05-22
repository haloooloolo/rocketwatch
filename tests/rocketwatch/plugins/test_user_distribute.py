from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.user_distribute import user_distribute as ud_module
from rocketwatch.plugins.user_distribute.user_distribute import UserDistribute
from tests.lib.discord_harness import make_bot, make_interaction
from tests.lib.scripted_rocketpool import ScriptedRocketPool

NOW = 1_700_000_000
WINDOW_START = 100
WINDOW_LENGTH = 200  # window: [start, start+length) = [100, 300)


def _make_cog(bot: Any) -> UserDistribute:
    # __init__ starts a tasks.loop; bypass it.
    cog = UserDistribute.__new__(UserDistribute)
    cog.bot = bot
    return cog


@pytest.fixture
def _stub_env(
    monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
) -> Iterator[dict[str, int]]:
    monkeypatch.setattr(ud_module.time, "time", lambda: NOW)
    monkeypatch.setattr(ud_module.w3, "to_checksum_address", lambda a: a, raising=False)
    scripted_rp.set_call(
        "rocketDAOProtocolSettingsMinipool.getUserDistributeWindowStart", WINDOW_START
    )
    scripted_rp.set_call(
        "rocketDAOProtocolSettingsMinipool.getUserDistributeWindowLength", WINDOW_LENGTH
    )
    # Per-address user_distribute_time, served via get_storage_at.
    storage: dict[str, int] = {}

    async def get_storage_at(address: str, _slot: int) -> bytes:
        return storage.get(address, 0).to_bytes(32, "big")

    monkeypatch.setattr(
        ud_module.w3, "eth", AsyncMock(get_storage_at=get_storage_at), raising=False
    )
    yield storage


def _minipool(address: str) -> dict[str, Any]:
    return {
        "address": address,
        "user_distributed": False,
        "status": "staking",
        "execution_balance": 10,
        "beacon": {"withdrawable_epoch": 1},  # below threshold for any head slot
    }


class TestFetchMinipools:
    async def test_classifies_pending_distributable_eligible(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_bacon: Any,
        _stub_env: dict[str, int],
    ) -> None:
        # head slot high enough that threshold_epoch > 1 (minipools qualify).
        scripted_bacon.set_block_header("head", {"slot": str(6000 * 32)})

        await mongo_db.minipools.insert_many(
            [_minipool("0xPEND"), _minipool("0xDIST"), _minipool("0xELIG")]
        )
        # elapsed = NOW - udt. pending: elapsed<100; distributable: 100<=e<300;
        # eligible: e>=300.
        _stub_env["0xPEND"] = NOW - 50
        _stub_env["0xDIST"] = NOW - 200
        _stub_env["0xELIG"] = NOW - 400
        # The eligible double-check: not yet distributed/finalised → stays eligible.
        scripted_rp.set_call("rocketMinipool.getUserDistributed", False)
        scripted_rp.set_call("rocketMinipool.getFinalised", False)

        cog = _make_cog(make_bot(db=mongo_db))
        eligible, pending, distributable = await cog._fetch_minipools()

        assert [m["address"] for m in pending] == ["0xPEND"]
        assert [m["address"] for m in distributable] == ["0xDIST"]
        assert [m["address"] for m in eligible] == ["0xELIG"]
        # Window fields are stamped for sorting/display.
        assert pending[0]["ud_window_open"] == (NOW - 50) + WINDOW_START
        assert distributable[0]["ud_window_close"] == (NOW - 200) + (
            WINDOW_START + WINDOW_LENGTH
        )

    async def test_eligible_skipped_when_already_distributed(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_bacon: Any,
        _stub_env: dict[str, int],
    ) -> None:
        scripted_bacon.set_block_header("head", {"slot": str(6000 * 32)})
        await mongo_db.minipools.insert_one(_minipool("0xDONE"))
        _stub_env["0xDONE"] = NOW - 400  # past the window → eligible branch
        # DB lagged: chain says it's already distributed → skip.
        scripted_rp.set_call("rocketMinipool.getUserDistributed", True)
        scripted_rp.set_call("rocketMinipool.getFinalised", False)

        cog = _make_cog(make_bot(db=mongo_db))
        eligible, pending, distributable = await cog._fetch_minipools()
        assert eligible == []
        assert pending == []
        assert distributable == []

    async def test_eligible_skipped_when_finalised(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_bacon: Any,
        _stub_env: dict[str, int],
    ) -> None:
        scripted_bacon.set_block_header("head", {"slot": str(6000 * 32)})
        await mongo_db.minipools.insert_one(_minipool("0xFIN"))
        _stub_env["0xFIN"] = NOW - 400
        scripted_rp.set_call("rocketMinipool.getUserDistributed", False)
        scripted_rp.set_call("rocketMinipool.getFinalised", True)

        cog = _make_cog(make_bot(db=mongo_db))
        eligible, _, _ = await cog._fetch_minipools()
        assert eligible == []


class TestUserDistributeStatusCommand:
    async def test_reports_all_three_categories(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_bacon: Any,
        _stub_env: dict[str, int],
    ) -> None:
        scripted_bacon.set_block_header("head", {"slot": str(6000 * 32)})
        await mongo_db.minipools.insert_many(
            [_minipool("0xPEND"), _minipool("0xDIST"), _minipool("0xELIG")]
        )
        _stub_env["0xPEND"] = NOW - 50
        _stub_env["0xDIST"] = NOW - 200
        _stub_env["0xELIG"] = NOW - 400
        scripted_rp.set_call("rocketMinipool.getUserDistributed", False)
        scripted_rp.set_call("rocketMinipool.getFinalised", False)

        cog = _make_cog(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.user_distribute_status.callback(cog, interaction)

        kwargs = interaction.followup.send.call_args.kwargs
        embed = kwargs["embed"]
        assert embed.title == "User Distribute Status"
        fields = {f.name: f.value for f in embed.fields}
        assert "1" in fields["Eligible"]
        assert "window opens" in fields["Pending"]
        assert "window closes" in fields["Distributable"]
        # eligible or distributable present → an InstructionsView is attached.
        assert "view" in kwargs

    async def test_empty_state_sends_plain_embed(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_bacon: Any,
        _stub_env: dict[str, int],
    ) -> None:
        scripted_bacon.set_block_header("head", {"slot": str(6000 * 32)})
        # No minipools at all.
        cog = _make_cog(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.user_distribute_status.callback(cog, interaction)

        kwargs = interaction.followup.send.call_args.kwargs
        # No eligible/distributable → no view attached.
        assert "view" not in kwargs
        fields = {f.name: f.value for f in kwargs["embed"].fields}
        assert fields["Eligible"] == "**0** minipools"
        assert fields["Pending"] == "**0** minipools"
        assert fields["Distributable"] == "**0** minipools"
