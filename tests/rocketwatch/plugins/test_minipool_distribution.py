from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.minipool_distribution.minipool_distribution import (
    MinipoolDistribution,
    get_percentiles,
    minipool_distribution_raw,
)
from tests.lib.discord_harness import make_bot, make_interaction


class TestGetPercentiles:
    def test_yields_one_value_per_percentile(self) -> None:
        out = list(get_percentiles([50, 90], [1, 2, 3, 4, 5]))
        assert [p for p, _ in out] == [50, 90]

    def test_nearest_method_returns_data_point(self) -> None:
        _, median = next(get_percentiles([50], [1, 2, 3, 4, 5]))
        assert median in (3,)


class TestMinipoolDistributionRaw:
    async def test_renders_singular_and_plural(self) -> None:
        interaction = make_interaction()
        await minipool_distribution_raw(interaction, [(1, 1), (3, 5)])
        interaction.followup.send.assert_awaited_once()
        desc = interaction.followup.send.call_args.kwargs["embed"].description
        assert "1 minipool:" in desc
        assert "1 node" in desc
        assert "3 minipools:" in desc
        assert "5 nodes" in desc


async def _seed_minipools(mongo_db: AsyncDatabase[dict[str, Any]]) -> None:
    # node A: 3 staking minipools, node B: 1, node C: 2.
    docs = []
    for node, n in (("0xA", 3), ("0xB", 1), ("0xC", 2)):
        docs += [
            {
                "node_operator": node,
                "status": "staking",
                "beacon": {"status": "active_ongoing"},
            }
            for _ in range(n)
        ]
    # Excluded by the $match: a withdrawn minipool and a non-staking one.
    docs.append(
        {
            "node_operator": "0xA",
            "status": "staking",
            "beacon": {"status": "withdrawal_done"},
        }
    )
    docs.append(
        {
            "node_operator": "0xB",
            "status": "initialised",
            "beacon": {"status": "active_ongoing"},
        }
    )
    await mongo_db.minipools.insert_many(docs)


class TestGetMinipoolCountsPerNode:
    async def test_counts_only_active_staking_minipools(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await _seed_minipools(mongo_db)
        cog = MinipoolDistribution(make_bot(db=mongo_db))
        counts = await cog.get_minipool_counts_per_node()
        # Sorted ascending: B=1, C=2, A=3.
        assert counts == [1, 2, 3]


class TestMinipoolDistributionCommand:
    async def test_image_path(self, mongo_db: AsyncDatabase[dict[str, Any]]) -> None:
        await _seed_minipools(mongo_db)
        cog = MinipoolDistribution(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.minipool_distribution.callback(cog, interaction)

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs["embed"].title == "Minipool Distribution"
        assert kwargs["files"][0].filename == "graph.png"
        footer = kwargs["embed"].footer.text
        assert "Total: 6 minipools" in footer
        assert "Max: 3 minipools per node" in footer

    async def test_raw_path(self, mongo_db: AsyncDatabase[dict[str, Any]]) -> None:
        await _seed_minipools(mongo_db)
        cog = MinipoolDistribution(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.minipool_distribution.callback(cog, interaction, raw=True)

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        assert "files" not in kwargs
        assert "minipool" in kwargs["embed"].description


class TestNodeGini:
    async def test_image_path_reports_gini(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await _seed_minipools(mongo_db)
        cog = MinipoolDistribution(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.node_gini.callback(cog, interaction)

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs["embed"].title == "Validator Share of Largest Nodes"
        assert "Gini coefficient" in kwargs["embed"].footer.text
        assert kwargs["files"][0].filename == "graph.png"

    async def test_raw_path_lists_thresholds(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await _seed_minipools(mongo_db)
        cog = MinipoolDistribution(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.node_gini.callback(cog, interaction, raw=True)

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        assert "files" not in kwargs
        desc = kwargs["embed"].description
        assert "Total:" in desc
        assert "nodes" in desc
