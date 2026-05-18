import pytest

from rocketwatch.utils.shared_w3 import bacon
from tests.lib.beacon_script import ScriptedBeacon, make_validator_record


class TestRegisterValidator:
    async def test_lookup_by_pubkey(self, scripted_bacon: ScriptedBeacon) -> None:
        scripted_bacon.register_validator(
            make_validator_record(pubkey="0xaa", index=7, status="active_ongoing")
        )
        resp = await bacon.get_validator("0xaa")
        assert resp["data"]["index"] == "7"

    async def test_lookup_by_index(self, scripted_bacon: ScriptedBeacon) -> None:
        scripted_bacon.register_validator(
            make_validator_record(pubkey="0xbb", index=42, status="active_ongoing")
        )
        resp = await bacon.get_validator(42)
        assert resp["data"]["validator"]["pubkey"] == "0xbb"

    async def test_unscripted_raises(self, scripted_bacon: ScriptedBeacon) -> None:
        with pytest.raises(KeyError):
            await bacon.get_validator("0xmissing")


class TestValidatorsByIds:
    async def test_filters_to_requested_ids(
        self, scripted_bacon: ScriptedBeacon
    ) -> None:
        scripted_bacon.register_validators(
            [
                make_validator_record(pubkey="0xaa", index=1),
                make_validator_record(pubkey="0xbb", index=2),
                make_validator_record(pubkey="0xcc", index=3),
            ]
        )
        resp = await bacon.get_validators_by_ids("head", ["0xaa", "0xcc"])
        assert sorted(d["validator"]["pubkey"] for d in resp["data"]) == [
            "0xaa",
            "0xcc",
        ]

    async def test_unknown_ids_are_dropped(
        self, scripted_bacon: ScriptedBeacon
    ) -> None:
        scripted_bacon.register_validator(make_validator_record(pubkey="0xaa", index=1))
        resp = await bacon.get_validators_by_ids("head", ["0xaa", "0xmissing"])
        assert len(resp["data"]) == 1


class TestBlocksAndHeaders:
    async def test_set_block_returns_message_envelope(
        self, scripted_bacon: ScriptedBeacon
    ) -> None:
        scripted_bacon.set_block("head", {"slot": "123"})
        resp = await bacon.get_block("head")
        assert resp["data"]["message"]["slot"] == "123"

    async def test_set_block_can_raise(self, scripted_bacon: ScriptedBeacon) -> None:
        scripted_bacon.set_block("99", RuntimeError("missed"))
        with pytest.raises(RuntimeError, match="missed"):
            await bacon.get_block("99")

    async def test_block_header_envelope(self, scripted_bacon: ScriptedBeacon) -> None:
        scripted_bacon.set_block_header("head", {"slot": "200"})
        resp = await bacon.get_block_header("head")
        assert resp["data"]["header"]["message"]["slot"] == "200"


class TestFinalityAndDuties:
    async def test_finality_checkpoint(self, scripted_bacon: ScriptedBeacon) -> None:
        scripted_bacon.set_finality_checkpoint("100", {"finalized": {"epoch": "3"}})
        resp = await bacon.get_finality_checkpoint("100")
        assert resp["data"]["finalized"]["epoch"] == "3"

    async def test_proposer_duties(self, scripted_bacon: ScriptedBeacon) -> None:
        scripted_bacon.set_proposer_duties(
            "5", [{"slot": "160", "validator_index": "7"}]
        )
        resp = await bacon.get_block_proposer_duties("5")
        assert resp["data"][0]["validator_index"] == "7"

    async def test_sync_committee(self, scripted_bacon: ScriptedBeacon) -> None:
        scripted_bacon.set_sync_committee(10, {"validators": ["1", "2", "3"]})
        resp = await bacon.get_sync_committee(10)
        assert resp["data"]["validators"] == ["1", "2", "3"]
