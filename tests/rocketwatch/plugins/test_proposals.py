import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientResponseError, RequestInfo
from pymongo.asynchronous.database import AsyncDatabase
from yarl import URL

from rocketwatch.plugins.proposals.proposals import (
    Proposals,
    parse_proposal,
)
from rocketwatch.utils.solidity import date_to_beacon_block
from tests.lib.beacon_script import ScriptedBeacon
from tests.lib.discord_harness import make_bot, make_interaction, run_command


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

    def test_smartnode_graffiti_single_client_letter(self) -> None:
        # A single letter after "RP-" is read as the consensus client; execution
        # stays at the template default.
        out = parse_proposal(
            _beacon_block(slot=15, proposer_index=6, graffiti="RP-L v2.0.0")
        )
        assert out["type"] == "Smart Node"
        assert out["consensus_client"] == "Lighthouse"
        assert out["execution_client"] == "Unknown"

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


class TestCheckIndexes:
    async def test_creates_unique_slot_index(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        bot = make_bot(db=mongo_db)
        bot.wait_until_ready = AsyncMock()
        cog = _make_cog(bot)
        await cog.check_indexes()
        idx = await mongo_db.proposals.index_information()
        assert any(v["key"] == [("slot", 1)] and v.get("unique") for v in idx.values())


class TestFetchProposals:
    async def test_advances_last_checked_slot(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        await mongo_db.last_checked_block.insert_one({"_id": "proposals", "slot": 100})
        scripted_bacon.set_block_header("finalized", {"slot": "102"})
        # Neither slot belongs to an RP validator → fetch_proposal returns right
        # after the header lookup, writing nothing.
        scripted_bacon.set_block_header("101", {"proposer_index": "9999"})
        scripted_bacon.set_block_header("102", {"proposer_index": "9999"})

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.fetch_proposals()

        entry = await mongo_db.last_checked_block.find_one({"_id": "proposals"})
        assert entry is not None and entry["slot"] == 102
        assert await mongo_db.proposals.count_documents({}) == 0

    async def test_falls_back_to_pre_merge_slot(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_bacon: ScriptedBeacon,
    ) -> None:
        # No last_checked_block doc → starts from the hardcoded pre-merge slot.
        scripted_bacon.set_block_header("finalized", {"slot": "4700013"})
        scripted_bacon.set_block_header("4700013", {"proposer_index": "9999"})

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.fetch_proposals()

        entry = await mongo_db.last_checked_block.find_one({"_id": "proposals"})
        assert entry is not None and entry["slot"] == 4700013


class TestGatherAttribute:
    async def test_merges_counts_by_attribute(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.latest_proposals.insert_many(
            [
                {
                    "latest_proposal": {
                        "consensus_client": "Lighthouse",
                        "type": "Smart Node",
                    },
                    "validator_count": 3,
                },
                {
                    "latest_proposal": {
                        "consensus_client": "Prysm",
                        "type": "Smart Node",
                    },
                    "validator_count": 2,
                },
            ]
        )
        cog = _make_cog(make_bot(db=mongo_db))
        d = await cog.gather_attribute("consensus_client")
        assert d["Lighthouse"]["validator_count"] == 3
        assert d["Prysm"]["validator_count"] == 2

    async def test_same_attribute_across_types_is_merged(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        # Same client under two operator types must collapse to one bucket.
        await mongo_db.latest_proposals.insert_many(
            [
                {
                    "latest_proposal": {
                        "consensus_client": "Lighthouse",
                        "type": "Smart Node",
                    },
                    "validator_count": 3,
                },
                {
                    "latest_proposal": {
                        "consensus_client": "Lighthouse",
                        "type": "Allnodes",
                    },
                    "validator_count": 2,
                },
            ]
        )
        cog = _make_cog(make_bot(db=mongo_db))
        d = await cog.gather_attribute("consensus_client")
        assert d["Lighthouse"]["count"] == 2
        assert d["Lighthouse"]["validator_count"] == 5

    async def test_remove_allnodes_filters_and_adds_sentinel(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.latest_proposals.insert_many(
            [
                {
                    "latest_proposal": {
                        "consensus_client": "Lighthouse",
                        "type": "Smart Node",
                    },
                    "validator_count": 3,
                },
                {
                    "latest_proposal": {"consensus_client": "Teku", "type": "Allnodes"},
                    "validator_count": 9,
                },
            ]
        )
        cog = _make_cog(make_bot(db=mongo_db))
        d = await cog.gather_attribute("consensus_client", remove_allnodes=True)
        assert "remove_from_total" in d
        assert "Lighthouse" in d
        assert "Teku" not in d  # the Allnodes entry is filtered out


class TestCreateLatestProposalView:
    async def test_view_groups_latest_proposal_per_operator(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.megapool_validators.insert_one(
            {
                "node_operator": "0xNO1",
                "beacon": {"status": "active_ongoing"},
                "validator_index": 1,
            }
        )
        await mongo_db.minipools.insert_one(
            {
                "node_operator": "0xNO1",
                "beacon": {"status": "active_ongoing"},
                "validator_index": 2,
            }
        )
        await mongo_db.proposals.insert_many(
            [
                {"validator": 1, "slot": 50, "type": "Smart Node"},
                {"validator": 2, "slot": 60, "type": "Smart Node"},
            ]
        )

        cog = _make_cog(make_bot(db=mongo_db))
        await cog.create_latest_proposal_view()

        docs = await mongo_db.latest_proposals.find().to_list(None)
        assert len(docs) == 1
        assert docs[0]["_id"] == "0xNO1"
        assert docs[0]["validator_count"] == 2
        assert docs[0]["latest_proposal"] is not None


class TestClientComboRanking:
    async def test_ranks_client_pairs(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.latest_proposals.insert_many(
            [
                {
                    "latest_proposal": {
                        "consensus_client": "Lighthouse",
                        "execution_client": "Geth",
                        "type": "Smart Node",
                    },
                    "validator_count": 5,
                },
                {
                    "latest_proposal": {
                        "consensus_client": "Prysm",
                        "execution_client": "Nethermind",
                        "type": "Smart Node",
                    },
                    "validator_count": 3,
                },
            ]
        )
        cog = _make_cog(make_bot(db=mongo_db))
        embed = await run_command(cog, "client_combo_ranking", make_interaction())
        assert embed.description is not None
        assert "Lighthouse" in embed.description
        assert "Geth" in embed.description


class TestDistributionCharts:
    async def _seed(self, mongo_db: AsyncDatabase[dict[str, Any]]) -> None:
        # plot_axes_with_data explicitly reorders an "Unknown" bucket and crashes
        # if absent, so the seed must include one.
        await mongo_db.latest_proposals.insert_many(
            [
                {
                    "latest_proposal": {
                        "type": "Smart Node",
                        "consensus_client": "Lighthouse",
                        "execution_client": "Geth",
                    },
                    "validator_count": 4,
                },
                {
                    "latest_proposal": {
                        "type": "Unknown",
                        "consensus_client": "Unknown",
                        "execution_client": "Unknown",
                    },
                    "validator_count": 1,
                },
            ]
        )
        await mongo_db.minipools.insert_many(
            [
                {
                    "_id": i,
                    "node_operator": f"0xNO{i}",
                    "beacon": {"status": "active_ongoing"},
                    "status": "staking",
                }
                for i in range(5)
            ]
        )

    async def test_operator_type_distribution_sends_image(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await self._seed(mongo_db)
        cog = _make_cog(make_bot(db=mongo_db))
        embed = await run_command(cog, "operator_type_distribution", make_interaction())
        assert embed.image.url == "attachment://type.png"

    async def test_client_distribution_sends_images(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await self._seed(mongo_db)
        cog = _make_cog(make_bot(db=mongo_db))
        interaction = make_interaction()
        # Sends plural embeds=/files=, which the embed-capture helper can't read;
        # invoke directly and assert the attachments were sent. The Any alias
        # sidesteps discord's command-callback typing.
        cmd: Any = cog.client_distribution
        await cmd.callback(cog, interaction, remove_allnodes=False)
        call = interaction.followup.send.call_args
        assert call is not None
        assert len(call.kwargs["files"]) == 2

    async def test_client_distribution_remove_allnodes_with_external(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        # Exercises the remove_allnodes subtraction and the "External" reorder
        # branch in plot_axes_with_data.
        await mongo_db.latest_proposals.insert_many(
            [
                {
                    "latest_proposal": {
                        "type": "Smart Node",
                        "consensus_client": "Lighthouse",
                        "execution_client": "Geth",
                    },
                    "validator_count": 4,
                },
                {
                    "latest_proposal": {
                        "type": "Smart Node",
                        "consensus_client": "Prysm",
                        "execution_client": "External",
                    },
                    "validator_count": 2,
                },
                {
                    "latest_proposal": {
                        "type": "Unknown",
                        "consensus_client": "Unknown",
                        "execution_client": "Unknown",
                    },
                    "validator_count": 1,
                },
                {
                    "latest_proposal": {
                        "type": "Allnodes",
                        "consensus_client": "Teku",
                        "execution_client": "Besu",
                    },
                    "validator_count": 9,
                },
            ]
        )
        await mongo_db.minipools.insert_many(
            [
                {
                    "_id": i,
                    "node_operator": f"0xNO{i}",
                    "beacon": {"status": "active_ongoing"},
                    "status": "staking",
                }
                for i in range(20)
            ]
        )
        cog = _make_cog(make_bot(db=mongo_db))
        interaction = make_interaction()
        cmd: Any = cog.client_distribution
        await cmd.callback(cog, interaction, remove_allnodes=True)
        call = interaction.followup.send.call_args
        assert call is not None
        assert len(call.kwargs["files"]) == 2

    async def test_version_chart_sends_image(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        slot = date_to_beacon_block(int(time.time()))
        await mongo_db.proposals.insert_many(
            [
                {"slot": slot - 20, "version": "1.0.0", "validator": 1},
                {"slot": slot - 10, "version": "1.0.0", "validator": 2},
                {"slot": slot, "version": "1.0.1", "validator": 3},
            ]
        )
        cog = _make_cog(make_bot(db=mongo_db))
        embed = await run_command(cog, "version_chart", make_interaction(), days=90)
        assert embed.image.url == "attachment://chart.png"
