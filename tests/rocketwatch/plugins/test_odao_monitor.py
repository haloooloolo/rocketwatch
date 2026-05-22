from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.odao_monitor import odao_monitor
from rocketwatch.plugins.odao_monitor.odao_monitor import (
    DUTIES,
    META_ID,
    ODAOMonitor,
    _fetch_duty_state,
)
from rocketwatch.utils import shared_w3
from tests.lib.discord_harness import make_bot
from tests.lib.scripted_rocketpool import ScriptedRocketPool, addr

BALANCES_DUTY = next(d for d in DUTIES if d.id == "balances")


def _make_cog(bot: Any) -> ODAOMonitor:
    # __init__ starts a tasks.loop; bypass it.
    cog = ODAOMonitor.__new__(ODAOMonitor)
    cog.bot = bot
    cog.collection = bot.db.odao_monitor
    return cog


@pytest.fixture(autouse=True)
def _stub_el_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def fake_el(target: str, *_: Any, **__: Any) -> str:
        return f"[{target}](el/{target})"

    monkeypatch.setattr(odao_monitor, "el_explorer_url", fake_el)
    monkeypatch.setattr(
        odao_monitor.w3, "to_checksum_address", lambda a: a, raising=False
    )
    yield


class TestFetchDutyState:
    async def test_returns_block_time_and_period(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripted_rp.set_call("rocketNetworkBalances.getBalancesBlock", 19_000_000)
        scripted_rp.set_call(
            "rocketDAOProtocolSettingsNetwork.getSubmitBalancesFrequency", 5760
        )

        async def get_block(_b: int) -> dict[str, int]:
            return {"timestamp": 1_700_000_000}

        monkeypatch.setattr(
            shared_w3.w3._instance, "eth", AsyncMock(get_block=get_block)
        )

        block, dt, period = await _fetch_duty_state(BALANCES_DUTY)
        assert block == 19_000_000
        assert dt.timestamp() == 1_700_000_000
        assert period.total_seconds() == 5760


class TestGetMemberAddresses:
    async def test_resolves_all_members(self, scripted_rp: ScriptedRocketPool) -> None:
        scripted_rp.set_call("rocketDAONodeTrusted.getMemberCount", 3)
        members = [addr(f"0x{i:040d}") for i in range(3)]
        scripted_rp.set_call("rocketDAONodeTrusted.getMemberAt", lambda i: members[i])
        cog = _make_cog(make_bot(db=None))
        out = await cog._get_member_addresses()
        assert out == members


class TestGetInactiveMembers:
    async def test_flags_members_below_threshold(
        self,
        scripted_rp: ScriptedRocketPool,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        members = [addr("0xA"), addr("0xB"), addr("0xC")]
        scripted_rp.set_call("rocketDAONodeTrusted.getMemberCount", len(members))
        scripted_rp.set_call("rocketDAONodeTrusted.getMemberAt", lambda i: members[i])
        latest_block = 20_000_000
        # MEMBER_INACTIVITY=3d at 12s/block ≈ 21600 blocks → threshold ≈ 19_978_400.
        await mongo_db.odao_monitor.insert_many(
            [
                # A: recent on both → active.
                {
                    "_id": "0xA",
                    "last_balance_block": latest_block - 100,
                    "last_price_block": latest_block - 100,
                },
                # B: stale balances, recent prices → missed balances only.
                {
                    "_id": "0xB",
                    "last_balance_block": 1_000_000,
                    "last_price_block": latest_block - 100,
                },
                # C: no record at all → missed both (block 0).
            ]
        )

        cog = _make_cog(make_bot(db=mongo_db))
        missed_bal, missed_price = await cog._get_inactive_members(latest_block)
        assert {a for a, _ in missed_bal} == {"0xB", "0xC"}
        assert {a for a, _ in missed_price} == {"0xC"}
        # Sorted ascending by last block (0 before 1_000_000).
        assert missed_bal[0][1] <= missed_bal[1][1]


class TestIngestSubmissions:
    async def test_records_latest_block_per_member(
        self,
        scripted_rp: ScriptedRocketPool,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Bypass the w3/process_log chain: feed decoded logs straight in.
        async def fake_get_logs(
            event: Any, *_a: Any, **_k: Any
        ) -> list[dict[str, Any]]:
            if event.event_name == "BalancesSubmitted":
                return [
                    {"args": {"from": "0xA"}, "blockNumber": 100},
                    {"args": {"from": "0xA"}, "blockNumber": 200},  # newer wins
                    {"args": {"from": "0xB"}, "blockNumber": 150},
                ]
            return [{"args": {"from": "0xA"}, "blockNumber": 175}]

        monkeypatch.setattr(odao_monitor, "get_logs", fake_get_logs)

        cog = _make_cog(make_bot(db=mongo_db))
        await cog._ingest_submissions(latest_block=20_000_000)

        a_doc = await mongo_db.odao_monitor.find_one({"_id": "0xA"})
        assert a_doc is not None
        assert a_doc["last_balance_block"] == 200
        assert a_doc["last_price_block"] == 175
        b_doc = await mongo_db.odao_monitor.find_one({"_id": "0xB"})
        assert b_doc is not None
        assert b_doc["last_balance_block"] == 150
        # Meta cursor advanced.
        meta = await mongo_db.odao_monitor.find_one({"_id": META_ID})
        assert meta is not None
        assert meta["last_scanned_block"] == 20_000_000

    async def test_uses_max_so_older_blocks_dont_regress(
        self,
        scripted_rp: ScriptedRocketPool,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.odao_monitor.insert_one(
            {"_id": "0xA", "last_balance_block": 500}
        )

        async def fake_get_logs(
            event: Any, *_a: Any, **_k: Any
        ) -> list[dict[str, Any]]:
            if event.event_name == "BalancesSubmitted":
                return [{"args": {"from": "0xA"}, "blockNumber": 300}]  # older
            return []

        monkeypatch.setattr(odao_monitor, "get_logs", fake_get_logs)

        cog = _make_cog(make_bot(db=mongo_db))
        await cog._ingest_submissions(latest_block=20_000_000)

        a_doc = await mongo_db.odao_monitor.find_one({"_id": "0xA"})
        assert a_doc is not None
        # $max keeps the higher existing value.
        assert a_doc["last_balance_block"] == 500

    async def test_skips_when_cursor_ahead_of_latest(
        self,
        scripted_rp: ScriptedRocketPool,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.odao_monitor.insert_one(
            {"_id": META_ID, "last_scanned_block": 20_000_000}
        )
        called = False

        async def fake_get_logs(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
            nonlocal called
            called = True
            return []

        monkeypatch.setattr(odao_monitor, "get_logs", fake_get_logs)
        cog = _make_cog(make_bot(db=mongo_db))
        await cog._ingest_submissions(latest_block=19_000_000)
        assert called is False


class TestAddPendingSubmissionsFields:
    async def test_groups_by_value_and_lists_non_submitters(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from rocketwatch.utils.embeds import Embed

        # Two members agree on a value, one submits a different value, one
        # member hasn't submitted at all.
        async def fake_get_logs(
            event: Any, *_a: Any, **_k: Any
        ) -> list[dict[str, Any]]:
            return [
                {
                    "args": {
                        "from": "0xA",
                        "block": 100,
                        "slotTimestamp": 1,
                        "totalEth": 10,
                        "stakingEth": 5,
                        "rethSupply": 8,
                    },
                    "blockNumber": 1000,
                },
                {
                    "args": {
                        "from": "0xB",
                        "block": 100,
                        "slotTimestamp": 1,
                        "totalEth": 10,
                        "stakingEth": 5,
                        "rethSupply": 8,
                    },
                    "blockNumber": 1001,
                },
                {
                    "args": {
                        "from": "0xC",
                        "block": 100,
                        "slotTimestamp": 1,
                        "totalEth": 999,  # disagrees
                        "stakingEth": 5,
                        "rethSupply": 8,
                    },
                    "blockNumber": 1002,
                },
            ]

        monkeypatch.setattr(odao_monitor, "get_logs", fake_get_logs)

        embed = Embed()
        members = ["0xA", "0xB", "0xC", "0xD"]
        await odao_monitor._add_pending_submissions_fields(
            embed,
            BALANCES_DUTY,
            last_consensus_block=50,
            latest_block=2000,
            members=members,
        )
        field_names = [f.name for f in embed.fields]
        # Two submission groups (2 votes, then 1 vote) + a no-submission group.
        assert any("Submission Group 1 (2 votes)" in n for n in field_names)
        assert any("Submission Group 2 (1 vote)" in n for n in field_names)
        assert any("No Submission (1)" in n for n in field_names)

    async def test_ignores_submissions_at_or_below_consensus_block(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from rocketwatch.utils.embeds import Embed

        async def fake_get_logs(
            event: Any, *_a: Any, **_k: Any
        ) -> list[dict[str, Any]]:
            # args.block == last_consensus_block → not pending.
            return [
                {
                    "args": {
                        "from": "0xA",
                        "block": 50,
                        "slotTimestamp": 1,
                        "totalEth": 10,
                        "stakingEth": 5,
                        "rethSupply": 8,
                    },
                    "blockNumber": 1000,
                }
            ]

        monkeypatch.setattr(odao_monitor, "get_logs", fake_get_logs)
        embed = Embed()
        await odao_monitor._add_pending_submissions_fields(
            embed,
            BALANCES_DUTY,
            last_consensus_block=50,
            latest_block=2000,
            members=["0xA"],
        )
        # No pending submissions → only the "No Submission" group.
        field_names = [f.name for f in embed.fields]
        assert field_names == ["No Submission (1)"]
