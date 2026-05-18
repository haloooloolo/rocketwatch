from collections.abc import Iterator
from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.lottery.lottery import Lottery
from tests.lib.beacon_script import ScriptedBeacon
from tests.lib.discord_harness import make_bot, make_interaction


@pytest.fixture(autouse=True)
def _stub_explorer(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def fake_el(target: str, *, prefix: str = "", name: str | None = None) -> str:
        return f"[{prefix}{target}](el/{target})"

    monkeypatch.setattr("rocketwatch.plugins.lottery.lottery.el_explorer_url", fake_el)
    yield


class TestGetSyncCommitteeData:
    async def test_filters_to_rp_validators(
        self,
        scripted_bacon: ScriptedBeacon,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        period = 5
        scripted_bacon.set_sync_committee(
            period * 256,
            # Five validators in the sync committee — only some are RP-tracked.
            {"validators": ["1", "2", "3", "100", "101"]},
        )
        await mongo_db.minipools.insert_many(
            [
                {
                    "validator_index": 1,
                    "pubkey": "0x01",
                    "node_operator": "0x" + "a" * 40,
                },
                # No node_operator — should be filtered out.
                {"validator_index": 2, "pubkey": "0x02", "node_operator": None},
            ]
        )
        await mongo_db.megapool_validators.insert_one(
            {
                "validator_index": 3,
                "pubkey": "0x03",
                "node_operator": "0x" + "b" * 40,
            }
        )

        cog = Lottery(make_bot(db=mongo_db))
        data = await cog.get_sync_committee_data(period)
        assert data["start_epoch"] == period * 256
        assert data["end_epoch"] == (period + 1) * 256
        # Only validators 1 + 3 survive (2 dropped for null node_operator; 100/101
        # absent from both collections).
        assert sorted(v["validator"] for v in data["validators"]) == [1, 3]


class TestGenerateSyncCommitteeDescription:
    async def test_includes_participation_and_validator_list(
        self,
        scripted_bacon: ScriptedBeacon,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        period = 7
        scripted_bacon.set_sync_committee(period * 256, {"validators": ["42"]})
        await mongo_db.minipools.insert_one(
            {
                "validator_index": 42,
                "pubkey": "0x42",
                "node_operator": "0x" + "c" * 40,
            }
        )

        cog = Lottery(make_bot(db=mongo_db))
        desc = await cog.generate_sync_committee_description(period)
        assert "Rocket Pool Participation" in desc
        # 1 validator out of 512.
        assert f"1/{Lottery.COMMITTEE_SIZE}" in desc
        assert "`42`" in desc
        # The single node operator is listed once.
        assert "1x [0x" in desc


class TestLotteryCommand:
    async def test_sends_two_embeds_for_current_and_next(
        self,
        scripted_bacon: ScriptedBeacon,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        # Head slot 65_536 → period = 65_536 // 32 // 256 = 8.
        scripted_bacon.set_block("head", {"slot": "65536"})
        scripted_bacon.set_sync_committee(8 * 256, {"validators": []})
        scripted_bacon.set_sync_committee(9 * 256, {"validators": []})

        cog = Lottery(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.lottery.callback(cog, interaction)

        send = interaction.followup.send
        send.assert_awaited_once()
        embeds = send.call_args.kwargs["embeds"]
        assert [e.title for e in embeds] == [
            "Current Sync Committee",
            "Next Sync Committee",
        ]
