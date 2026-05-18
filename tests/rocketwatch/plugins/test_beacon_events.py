from collections.abc import Iterator
from typing import Any

import pytest
from aiohttp import ClientResponseError, RequestInfo
from pymongo.asynchronous.database import AsyncDatabase
from yarl import URL

from rocketwatch.plugins.beacon_events.beacon_events import (
    BeaconEvents,
    _build_finality_embed,
)
from tests.lib.beacon_script import ScriptedBeacon
from tests.lib.discord_harness import make_bot


def _not_found_error() -> ClientResponseError:
    return ClientResponseError(
        request_info=RequestInfo(
            url=URL("http://example"),
            method="GET",
            headers={},  # type: ignore[arg-type]
            real_url=URL("http://example"),
        ),
        history=(),
        status=404,
    )


def _make_cog(bot: Any) -> BeaconEvents:
    # The EventPlugin __init__ reads cfg + sets up state we don't need here.
    cog = BeaconEvents.__new__(BeaconEvents)
    cog.bot = bot
    cog.finality_delay_threshold = 3
    return cog


@pytest.fixture(autouse=True)
def _stub_explorer_helpers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # `_get_slashings` / `_get_missed_proposal` enrich embeds with sea-creature
    # holdings + el/cl explorer URLs. Stub the cosmetic bits so tests don't
    # have to script RPL prices, balances, and partner lookups.
    async def fake_sea(_address: str) -> str:
        return "🦀"

    async def fake_el(target: str, *, prefix: str = "", name: str | None = None) -> str:
        return f"[{prefix}{target}](el/{target})"

    async def fake_cl(target: int | str, name: str | None = None) -> str:
        return f"[{target}](cl/{target})"

    monkeypatch.setattr(
        "rocketwatch.plugins.beacon_events.beacon_events.get_sea_creature_for_address",
        fake_sea,
    )
    monkeypatch.setattr(
        "rocketwatch.plugins.beacon_events.beacon_events.el_explorer_url", fake_el
    )
    monkeypatch.setattr(
        "rocketwatch.plugins.beacon_events.beacon_events.cl_explorer_url", fake_cl
    )
    monkeypatch.setattr(
        "rocketwatch.plugins.beacon_events.beacon_events.w3.to_checksum_address",
        lambda a: a,
    )

    # `_get_missed_proposal` stamps each Event with a block_number derived
    # from `ts_to_block`. The real implementation binary-searches over
    # `w3.eth.get_block`, which we don't model here.
    async def fake_ts_to_block(ts: int) -> int:
        return 20_000_000

    monkeypatch.setattr(
        "rocketwatch.plugins.beacon_events.beacon_events.ts_to_block",
        fake_ts_to_block,
    )
    yield


def _make_block(
    *,
    slot: int,
    proposer_index: int,
    block_number: int,
    timestamp: int,
    attester_slashings: list[dict[str, Any]] | None = None,
    proposer_slashings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "slot": str(slot),
        "proposer_index": str(proposer_index),
        "body": {
            "attester_slashings": attester_slashings or [],
            "proposer_slashings": proposer_slashings or [],
            "execution_payload": {
                "block_number": str(block_number),
                "timestamp": str(timestamp),
            },
        },
    }


class TestBuildFinalityEmbed:
    def test_delay_event_uses_warning_title(self) -> None:
        embed = _build_finality_embed("finality_delay_event", 4, 100, 1_700_000_000)
        assert embed.title is not None
        assert "Finality Delay" in embed.title
        fields = {f.name: f.value for f in embed.fields}
        assert "100" in fields["Epoch"]

    def test_recover_event_uses_recover_title(self) -> None:
        embed = _build_finality_embed(
            "finality_delay_recover_event", 0, 101, 1_700_000_000
        )
        assert embed.title is not None
        assert "Recovered" in embed.title


class TestSlashings:
    async def test_attester_slashing_emits_event(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        # Validator 7 is in both attestation_1 and attestation_2 → slashed.
        block = _make_block(
            slot=100,
            proposer_index=9,
            block_number=20_000_000,
            timestamp=1_700_000_000,
            attester_slashings=[
                {
                    "attestation_1": {"attesting_indices": ["7", "8"]},
                    "attestation_2": {"attesting_indices": ["7", "11"]},
                }
            ],
        )
        await mongo_db.minipools.insert_one(
            {"validator_index": 7, "node_operator": "0x" + "1" * 40}
        )

        cog = _make_cog(make_bot(db=mongo_db))
        events = await cog._get_slashings(block)
        assert len(events) == 1
        assert events[0].event_name == "validator_slash_event"
        embed_fields = {f.name: f.value for f in events[0].embed.fields}
        assert "`Attestation Violation`" in embed_fields["Reason"]

    async def test_proposer_slashing_emits_event(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        block = _make_block(
            slot=200,
            proposer_index=9,
            block_number=20_000_001,
            timestamp=1_700_000_100,
            proposer_slashings=[
                {"signed_header_1": {"message": {"proposer_index": "55"}}}
            ],
        )
        # Slashed validator lives in megapool_validators.
        await mongo_db.megapool_validators.insert_one(
            {"validator_index": 55, "node_operator": "0x" + "2" * 40}
        )

        cog = _make_cog(make_bot(db=mongo_db))
        events = await cog._get_slashings(block)
        assert len(events) == 1
        embed_fields = {f.name: f.value for f in events[0].embed.fields}
        assert "`Proposal Violation`" in embed_fields["Reason"]

    async def test_unknown_validator_is_ignored(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        # Validator 99 is slashed but not in DB → skipped.
        block = _make_block(
            slot=300,
            proposer_index=1,
            block_number=20_000_002,
            timestamp=1_700_000_200,
            attester_slashings=[
                {
                    "attestation_1": {"attesting_indices": ["99"]},
                    "attestation_2": {"attesting_indices": ["99"]},
                }
            ],
        )
        cog = _make_cog(make_bot(db=mongo_db))
        events = await cog._get_slashings(block)
        assert events == []


class TestMissedProposal:
    async def test_rp_validator_missed_proposal_emits_event(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        slot = 320  # epoch 10
        scripted_bacon.set_proposer_duties(
            "10",
            [{"slot": str(slot), "validator_index": "77"}],
        )
        await mongo_db.minipools.insert_one(
            {"validator_index": 77, "node_operator": "0x" + "3" * 40}
        )

        cog = _make_cog(make_bot(db=mongo_db))
        event = await cog._get_missed_proposal(slot)
        assert event is not None
        assert event.event_name == "missed_proposal_event"
        assert f"missed_proposal:{slot}" in event.unique_id

    async def test_non_rp_validator_returns_none(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        scripted_bacon.set_proposer_duties(
            "10", [{"slot": "320", "validator_index": "12345"}]
        )
        cog = _make_cog(make_bot(db=mongo_db))
        assert await cog._get_missed_proposal(320) is None

    async def test_proposer_duties_failure_returns_none(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        scripted_bacon.set_proposer_duties("10", _not_found_error())
        cog = _make_cog(make_bot(db=mongo_db))
        assert await cog._get_missed_proposal(320) is None


class TestCheckFinality:
    async def test_delay_above_threshold_emits_event(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        # Slot 320 → epoch 10. finalized epoch = 6 → delay = 4 (>= threshold 3).
        scripted_bacon.set_finality_checkpoint("320", {"finalized": {"epoch": "6"}})
        block = _make_block(
            slot=320,
            proposer_index=1,
            block_number=20_000_000,
            timestamp=1_700_000_000,
        )

        cog = _make_cog(make_bot(db=mongo_db))
        event = await cog._check_finality(block)
        assert event is not None
        assert event.event_name == "finality_delay_event"
        # Persists the delay so the next check can compare against it.
        stored = await mongo_db.finality_checkpoints.find_one({"epoch": 10})
        assert stored is not None
        assert stored["finality_delay"] == 4

    async def test_recovery_emits_event(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        # Previous epoch saw delay 5 (>= threshold); this epoch is at delay 1.
        await mongo_db.finality_checkpoints.insert_one(
            {"epoch": 9, "finality_delay": 5}
        )
        scripted_bacon.set_finality_checkpoint("320", {"finalized": {"epoch": "9"}})
        block = _make_block(
            slot=320,
            proposer_index=1,
            block_number=20_000_000,
            timestamp=1_700_000_000,
        )

        cog = _make_cog(make_bot(db=mongo_db))
        event = await cog._check_finality(block)
        assert event is not None
        assert event.event_name == "finality_delay_recover_event"

    async def test_no_delay_no_event(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        scripted_bacon.set_finality_checkpoint("320", {"finalized": {"epoch": "10"}})
        block = _make_block(
            slot=320,
            proposer_index=1,
            block_number=20_000_000,
            timestamp=1_700_000_000,
        )
        cog = _make_cog(make_bot(db=mongo_db))
        assert await cog._check_finality(block) is None

    async def test_checkpoint_error_returns_none(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        scripted_bacon.set_finality_checkpoint("320", _not_found_error())
        block = _make_block(
            slot=320,
            proposer_index=1,
            block_number=20_000_000,
            timestamp=1_700_000_000,
        )
        cog = _make_cog(make_bot(db=mongo_db))
        assert await cog._check_finality(block) is None


class TestGetEventsForSlot:
    async def test_404_falls_through_to_missed_proposal(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        scripted_bacon.set_block("320", _not_found_error())
        scripted_bacon.set_proposer_duties(
            "10", [{"slot": "320", "validator_index": "77"}]
        )
        await mongo_db.minipools.insert_one(
            {"validator_index": 77, "node_operator": "0x" + "4" * 40}
        )

        cog = _make_cog(make_bot(db=mongo_db))
        events = await cog._get_events_for_slot(320, check_finality=False)
        assert len(events) == 1
        assert events[0].event_name == "missed_proposal_event"

    async def test_404_no_rp_validator_returns_empty(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        scripted_bacon.set_block("320", _not_found_error())
        scripted_bacon.set_proposer_duties(
            "10", [{"slot": "320", "validator_index": "12345"}]
        )
        cog = _make_cog(make_bot(db=mongo_db))
        assert await cog._get_events_for_slot(320, check_finality=False) == []

    async def test_other_http_errors_propagate(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        err = ClientResponseError(
            request_info=RequestInfo(
                url=URL("http://x"),
                method="GET",
                headers={},  # type: ignore[arg-type]
                real_url=URL("http://x"),
            ),
            history=(),
            status=500,
        )
        scripted_bacon.set_block("320", err)
        cog = _make_cog(make_bot(db=mongo_db))
        with pytest.raises(ClientResponseError):
            await cog._get_events_for_slot(320, check_finality=False)


class TestGetProposal:
    async def test_returns_none_without_api_key(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        # Baseline cfg leaves beaconcha_secret = "" → short-circuit.
        block = _make_block(
            slot=100,
            proposer_index=7,
            block_number=20_000_000,
            timestamp=1_700_000_000,
        )
        await mongo_db.minipools.insert_one(
            {"validator_index": 7, "node_operator": "0x" + "5" * 40}
        )
        cog = _make_cog(make_bot(db=mongo_db))
        # retry decorator wraps but the no-key branch returns None directly.
        assert await cog._get_proposal(block) is None  # type: ignore[arg-type]

    async def test_returns_none_for_non_rp_validator(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        block = _make_block(
            slot=100,
            proposer_index=12345,
            block_number=20_000_000,
            timestamp=1_700_000_000,
        )
        cog = _make_cog(make_bot(db=mongo_db))
        assert await cog._get_proposal(block) is None  # type: ignore[arg-type]
