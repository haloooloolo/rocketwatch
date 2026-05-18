from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.fee_distribution.fee_distribution import FeeDistribution
from tests.lib.discord_harness import (
    captured_embed,
    make_bot,
    make_interaction,
    run_command,
)

pytestmark = pytest.mark.integration_db


def _make_minipool(*, bond: int, fee: float, active: bool = True) -> dict[str, Any]:
    return {
        "node_deposit_balance": bond,
        "node_fee": fee,
        "beacon": {"status": "active_ongoing" if active else "withdrawal_done"},
    }


@pytest.fixture
def cog(mongo_db: AsyncDatabase[dict[str, Any]]) -> FeeDistribution:
    return FeeDistribution(make_bot(db=mongo_db))


async def _seed_two_bonds(db: AsyncDatabase[dict[str, Any]]) -> None:
    await db.minipools.insert_many(
        [
            _make_minipool(bond=8, fee=0.14),
            _make_minipool(bond=8, fee=0.14),
            _make_minipool(bond=8, fee=0.10),
            _make_minipool(bond=16, fee=0.20),
            # Should be filtered out — not active.
            _make_minipool(bond=8, fee=0.14, active=False),
        ]
    )


class TestTreeMode:
    async def test_tree_renders_grouped_by_bond_and_fee(
        self,
        cog: FeeDistribution,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        await _seed_two_bonds(mongo_db)

        interaction = make_interaction()
        embed = await run_command(cog, "fee_distribution", interaction, mode="tree")

        assert embed.title == "Minipool Fee Distribution"
        assert embed.description is not None
        # `render_tree_legacy` applies `.title()` casing, so "8 ETH" → "8 Eth".
        assert "8 Eth" in embed.description
        assert "16 Eth" in embed.description
        assert "14%" in embed.description
        assert "10%" in embed.description
        assert "20%" in embed.description
        # 14% count should be 2 (the inactive minipool was filtered out).
        line = next(line for line in embed.description.splitlines() if "14%" in line)
        assert "2" in line.rsplit(":", 1)[-1]


class TestPieMode:
    async def test_pie_attaches_png_file(
        self,
        cog: FeeDistribution,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        await _seed_two_bonds(mongo_db)

        interaction = make_interaction()
        # Default mode is 'pie' so no mode arg.
        await run_command(cog, "fee_distribution", interaction)

        embed = captured_embed(interaction)
        assert embed.title == "Minipool Fee Distribution"
        # The image is sent as a file attachment with a reference in the embed.
        assert embed.image.url is not None
        assert embed.image.url.startswith("attachment://")

        # And the followup.send call should include a file kwarg.
        call_kwargs = interaction.followup.send.call_args.kwargs
        assert "file" in call_kwargs
        assert call_kwargs["file"].filename == "fee_distribution.png"

    async def test_pie_handles_empty_dataset(
        self,
        cog: FeeDistribution,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        # Nothing to plot. Matplotlib should still produce an embed without
        # crashing (the cog has no guard, so this test pins current behaviour).
        interaction = make_interaction()
        await run_command(cog, "fee_distribution", interaction, mode="pie")
        embed = captured_embed(interaction)
        assert embed.title == "Minipool Fee Distribution"
