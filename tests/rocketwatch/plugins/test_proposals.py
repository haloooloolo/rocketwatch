from typing import Any

import pytest
from aiohttp import ClientResponseError, RequestInfo
from pymongo.asynchronous.database import AsyncDatabase
from yarl import URL

from rocketwatch.plugins.proposals.proposals import (
    Proposals,
    parse_proposal,
)
from tests.lib.beacon_script import ScriptedBeacon
from tests.lib.discord_harness import make_bot


def _make_cog(bot: Any) -> Proposals:
    # __init__ creates a cronitor monitor + schedules a real loop. Bypass it.
    cog = Proposals.__new__(Proposals)
    cog.bot = bot
    cog.batch_size = 5
    return cog


def _hex_graffiti(text: str) -> str:
    # The beacon node returns graffiti as 32-byte hex.
    raw = text.encode("utf-8").ljust(32, b"\x00")
    return "0x" + raw.hex()


def _beacon_block(
    *,
    slot: int,
    proposer_index: int,
    graffiti: str = "",
) -> dict[str, Any]:
    return {
        "slot": str(slot),
        "proposer_index": str(proposer_index),
        "body": {"graffiti": _hex_graffiti(graffiti)},
    }


class TestParseProposal:
    def test_smartnode_graffiti_extracts_client_pair(self) -> None:
        out = parse_proposal(
            _beacon_block(slot=10, proposer_index=1, graffiti="RP-GL v1.2.3")
        )
        assert out["type"] == "Smart Node"
        assert out["execution_client"] == "Geth"
        assert out["consensus_client"] == "Lighthouse"
        assert out["version"] == "1.2.3"

    def test_smartnode_graffiti_with_comment(self) -> None:
        out = parse_proposal(
            _beacon_block(
                slot=11,
                proposer_index=2,
                graffiti="RP-NP 1.0.0 (hello world)",
            )
        )
        assert out["execution_client"] == "Nethermind"
        assert out["consensus_client"] == "Prysm"
        assert out["comment"] == "hello world"

    def test_allnodes_graffiti(self) -> None:
        out = parse_proposal(
            _beacon_block(slot=12, proposer_index=3, graffiti="⚡️Allnodes")
        )
        assert out["type"] == "Allnodes"
        assert out["consensus_client"] == "Teku"
        assert out["execution_client"] == "Besu"

    def test_freeform_graffiti_detects_clients(self) -> None:
        out = parse_proposal(
            _beacon_block(
                slot=13,
                proposer_index=4,
                graffiti="reth + lodestar by foo",
            )
        )
        assert out["type"] == "Unknown"
        assert out["consensus_client"] == "Lodestar"
        assert out["execution_client"] == "Reth"

    def test_unrecognised_graffiti_leaves_template_defaults(self) -> None:
        out = parse_proposal(_beacon_block(slot=14, proposer_index=5, graffiti="hello"))
        assert out["type"] == "Unknown"
        assert out["consensus_client"] == "Unknown"
        assert out["execution_client"] == "Unknown"


class TestFetchProposal:
    async def test_404_block_header_is_swallowed(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        scripted_bacon.set_block_header(
            "100",
            ClientResponseError(
                request_info=RequestInfo(
                    url=URL("http://x"),
                    method="GET",
                    headers={},  # type: ignore[arg-type]
                    real_url=URL("http://x"),
                ),
                history=(),
                status=404,
            ),
        )
        cog = _make_cog(make_bot(db=mongo_db))
        # Returns silently, writes nothing.
        await cog.fetch_proposal(100)
        assert await mongo_db.proposals.count_documents({}) == 0

    async def test_other_http_errors_propagate(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        scripted_bacon.set_block_header(
            "100",
            ClientResponseError(
                request_info=RequestInfo(
                    url=URL("http://x"),
                    method="GET",
                    headers={},  # type: ignore[arg-type]
                    real_url=URL("http://x"),
                ),
                history=(),
                status=500,
            ),
        )
        cog = _make_cog(make_bot(db=mongo_db))
        with pytest.raises(ClientResponseError):
            await cog.fetch_proposal(100)

    async def test_non_rp_validator_skipped(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        scripted_bacon.set_block_header("100", {"proposer_index": "9999"})
        cog = _make_cog(make_bot(db=mongo_db))
        await cog.fetch_proposal(100)
        assert await mongo_db.proposals.count_documents({}) == 0

    async def test_rp_minipool_validator_records_proposal(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        await mongo_db.minipools.insert_one(
            {"validator_index": 42, "node_operator": "0x" + "a" * 40}
        )
        scripted_bacon.set_block_header("100", {"proposer_index": "42"})
        scripted_bacon.set_block(
            "100",
            {
                "slot": "100",
                "proposer_index": "42",
                "body": {"graffiti": _hex_graffiti("RP-GL v1.0.0")},
            },
        )

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.fetch_proposal(100)

        stored = await mongo_db.proposals.find_one({"slot": 100})
        assert stored is not None
        assert stored["validator"] == 42
        assert stored["type"] == "Smart Node"
        assert stored["consensus_client"] == "Lighthouse"
