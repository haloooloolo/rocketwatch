from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientResponseError, RequestInfo
from eth_typing import BlockNumber
from pymongo.asynchronous.database import AsyncDatabase
from yarl import URL

from rocketwatch.plugins.beacon_events import beacon_events as be
from rocketwatch.plugins.beacon_events.beacon_events import (
    BeaconEvents,
    _build_finality_embed,
)
from rocketwatch.utils import shared_w3 as sw
from rocketwatch.utils.config import cfg
from rocketwatch.utils.solidity import beacon_block_to_date
from tests.lib.beacon_script import ScriptedBeacon
from tests.lib.cfg import make_cfg
from tests.lib.discord_harness import make_bot
from tests.lib.scripted_rocketpool import ScriptedRocketPool

Db = AsyncDatabase[dict[str, Any]]
SMOOTH = "0x" + "a" * 40
OTHER = "0x" + "b" * 40


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
        assert "100" in (fields["Epoch"] or "")

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
        events = await cog._get_slashings(block)  # type: ignore[arg-type]
        assert len(events) == 1
        assert events[0].event_name == "validator_slash_event"
        embed_fields = {f.name: f.value for f in events[0].embed.fields}
        assert "`Attestation Violation`" in (embed_fields["Reason"] or "")

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
        events = await cog._get_slashings(block)  # type: ignore[arg-type]
        assert len(events) == 1
        embed_fields = {f.name: f.value for f in events[0].embed.fields}
        assert "`Proposal Violation`" in (embed_fields["Reason"] or "")

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
        events = await cog._get_slashings(block)  # type: ignore[arg-type]
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
        event = await cog._check_finality(block)  # type: ignore[arg-type]
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
        event = await cog._check_finality(block)  # type: ignore[arg-type]
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
        assert await cog._check_finality(block) is None  # type: ignore[arg-type]

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
        assert await cog._check_finality(block) is None  # type: ignore[arg-type]


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


# --- _get_proposal with an API key configured (exercises the beaconcha fetch) ---


class _FakeResp:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> dict[str, Any]:
        return self._payload


class _FakeSession:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResp:
        return self._resp


def _patch_session(monkeypatch: pytest.MonkeyPatch, resp: _FakeResp) -> None:
    monkeypatch.setattr("aiohttp.ClientSession", lambda: _FakeSession(resp))


def _proposal_payload(
    producer_reward: int,
    *,
    relay_recipient: str | None = None,
    fee_recipient: str = OTHER,
) -> dict[str, Any]:
    relay = {"producerFeeRecipient": relay_recipient} if relay_recipient else None
    return {
        "data": [
            {
                "producerReward": str(producer_reward),
                "relay": relay,
                "feeRecipient": fee_recipient,
            }
        ]
    }


@pytest.fixture
def beaconcha_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    c = make_cfg()
    c.consensus_layer.beaconcha_secret = "KEY"
    monkeypatch.setattr(cfg, "_instance", c)


class TestGetProposalBody:
    async def test_no_execution_payload_returns_none(
        self, mongo_db: Db, beaconcha_cfg: None
    ) -> None:
        block = {"slot": "100", "proposer_index": "7", "body": {}}
        cog = _make_cog(make_bot(db=mongo_db))
        assert await cog._get_proposal(block) is None  # type: ignore[arg-type]

    async def test_non_rp_validator_with_key_returns_none(
        self, mongo_db: Db, beaconcha_cfg: None
    ) -> None:
        block = _make_block(
            slot=100, proposer_index=999, block_number=20_000_000, timestamp=1_700_000
        )
        cog = _make_cog(make_bot(db=mongo_db))
        assert await cog._get_proposal(block) is None  # type: ignore[arg-type]

    async def test_low_reward_returns_none(
        self,
        mongo_db: Db,
        beaconcha_cfg: None,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.minipools.insert_one(
            {"validator_index": 7, "node_operator": OTHER}
        )
        scripted_rp.set_address("rocketSmoothingPool", SMOOTH)  # type: ignore[arg-type]
        _patch_session(monkeypatch, _FakeResp(200, _proposal_payload(5 * 10**17)))
        block = _make_block(
            slot=100, proposer_index=7, block_number=20_000_000, timestamp=1_700_000
        )
        cog = _make_cog(make_bot(db=mongo_db))
        assert await cog._get_proposal(block) is None  # type: ignore[arg-type]

    async def test_rate_limit_returns_none(
        self,
        mongo_db: Db,
        beaconcha_cfg: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.minipools.insert_one(
            {"validator_index": 7, "node_operator": OTHER}
        )
        _patch_session(monkeypatch, _FakeResp(429, {}))
        block = _make_block(
            slot=100, proposer_index=7, block_number=20_000_000, timestamp=1_700_000
        )
        cog = _make_cog(make_bot(db=mongo_db))
        assert await cog._get_proposal(block) is None  # type: ignore[arg-type]

    async def test_large_proposal_emits_event(
        self,
        mongo_db: Db,
        beaconcha_cfg: None,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.minipools.insert_one(
            {"validator_index": 7, "node_operator": OTHER}
        )
        scripted_rp.set_address("rocketSmoothingPool", SMOOTH)  # type: ignore[arg-type]
        _patch_session(
            monkeypatch,
            _FakeResp(200, _proposal_payload(3 * 10**18, fee_recipient=OTHER)),
        )
        block = _make_block(
            slot=100, proposer_index=7, block_number=20_000_000, timestamp=1_700_000
        )
        cog = _make_cog(make_bot(db=mongo_db))
        event = await cog._get_proposal(block)  # type: ignore[arg-type]
        assert event is not None
        assert event.event_name == "mev_proposal_event"

    async def test_smoothing_pool_proposal_includes_balance(
        self,
        mongo_db: Db,
        beaconcha_cfg: None,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.minipools.insert_one(
            {"validator_index": 7, "node_operator": OTHER}
        )
        scripted_rp.set_address("rocketSmoothingPool", SMOOTH)  # type: ignore[arg-type]
        _patch_session(
            monkeypatch,
            _FakeResp(200, _proposal_payload(3 * 10**18, relay_recipient=SMOOTH)),
        )
        monkeypatch.setattr(
            sw.w3.eth, "get_balance", AsyncMock(return_value=5 * 10**18)
        )
        block = _make_block(
            slot=100, proposer_index=7, block_number=20_000_000, timestamp=1_700_000
        )
        cog = _make_cog(make_bot(db=mongo_db))
        event = await cog._get_proposal(block)  # type: ignore[arg-type]
        assert event is not None
        assert event.event_name == "mev_proposal_smoothie_event"
        assert "Smoothing Pool Balance" in {f.name for f in event.embed.fields}


class TestGetPastEvents:
    async def test_processes_slot_range(
        self,
        mongo_db: Db,
        scripted_bacon: ScriptedBeacon,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ts_from = beacon_block_to_date(9)  # → from_slot 10
        ts_to = beacon_block_to_date(12)  # → to_slot 12
        monkeypatch.setattr(
            sw.w3.eth,
            "get_block",
            AsyncMock(side_effect=[{"timestamp": ts_from}, {"timestamp": ts_to}]),
        )
        await mongo_db.minipools.insert_one(
            {"validator_index": 7, "node_operator": "0x" + "7" * 40}
        )
        scripted_bacon.set_block(
            "10",
            _make_block(
                slot=10,
                proposer_index=1,
                block_number=20_000_010,
                timestamp=ts_from,
                attester_slashings=[
                    {
                        "attestation_1": {"attesting_indices": ["7"]},
                        "attestation_2": {"attesting_indices": ["7"]},
                    }
                ],
            ),
        )
        scripted_bacon.set_block(
            "12",
            _make_block(
                slot=12, proposer_index=1, block_number=20_000_012, timestamp=ts_to
            ),
        )
        scripted_bacon.set_finality_checkpoint("12", {"finalized": {"epoch": "0"}})

        cog = _make_cog(make_bot(db=mongo_db))
        events = await cog.get_past_events(BlockNumber(100), BlockNumber(200))

        assert any(e.event_name == "validator_slash_event" for e in events)

    async def test_get_new_events_delegates_to_past_events(
        self, mongo_db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cog = _make_cog(make_bot(db=mongo_db))
        cog.last_served_block = BlockNumber(100)
        cog.lookback_distance = 10
        cog._pending_block = BlockNumber(200)
        captured = AsyncMock(return_value=["E"])
        monkeypatch.setattr(cog, "get_past_events", captured)

        result = await cog._get_new_events()

        assert result == captured.return_value
        captured.assert_awaited_once_with(BlockNumber(91), BlockNumber(200))

    async def test_slot_collects_proposal_and_finality(
        self,
        mongo_db: Db,
        beaconcha_cfg: None,
        scripted_rp: ScriptedRocketPool,
        scripted_bacon: ScriptedBeacon,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await mongo_db.minipools.insert_one(
            {"validator_index": 7, "node_operator": OTHER}
        )
        scripted_rp.set_address("rocketSmoothingPool", SMOOTH)  # type: ignore[arg-type]
        _patch_session(monkeypatch, _FakeResp(200, _proposal_payload(3 * 10**18)))
        scripted_bacon.set_block(
            "320",
            _make_block(
                slot=320, proposer_index=7, block_number=20_000_000, timestamp=1_700_000
            ),
        )
        # epoch 10, finalized 6 → delay 4 ≥ threshold 3
        scripted_bacon.set_finality_checkpoint("320", {"finalized": {"epoch": "6"}})

        cog = _make_cog(make_bot(db=mongo_db))
        events = await cog._get_events_for_slot(320, check_finality=True)

        names = {e.event_name for e in events}
        assert "mev_proposal_event" in names
        assert "finality_delay_event" in names


class TestSetup:
    async def test_registers_cog(self) -> None:
        bot = make_bot()
        bot.add_cog = AsyncMock()
        await be.setup(bot)
        bot.add_cog.assert_awaited_once()
