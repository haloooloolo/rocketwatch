from typing import Any
from unittest.mock import AsyncMock

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.delegate_contracts.delegate_contracts import DelegateContracts
from tests.lib.discord_harness import (
    make_bot,
    make_interaction,
    run_command,
)
from tests.lib.scripted_rocketpool import ScriptedRocketPool, addr

pytestmark = pytest.mark.integration_db


LATEST_MINIPOOL = addr("0x" + "10" * 20)
OLD_MINIPOOL = addr("0x" + "20" * 20)
LATEST_MEGAPOOL = addr("0x" + "30" * 20)
OLD_MEGAPOOL = addr("0x" + "40" * 20)


@pytest.fixture
def patch_w3_and_explorer(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cog calls `w3.to_checksum_address` to normalise stored addresses and
    # `el_explorer_url` to format them. Both need deterministic stand-ins so the
    # rendered embed is predictable.
    monkeypatch.setattr(
        "rocketwatch.plugins.delegate_contracts.delegate_contracts.w3.to_checksum_address",
        lambda a: a,
    )

    async def fake_explorer(target: str, name: str, *_: Any, **__: Any) -> str:
        return f"[{name}](explorer/{target})"

    monkeypatch.setattr(
        "rocketwatch.plugins.delegate_contracts.delegate_contracts.el_explorer_url",
        fake_explorer,
    )


@pytest.fixture
def cog(
    mongo_db: AsyncDatabase[dict[str, Any]],
    scripted_rp: ScriptedRocketPool,
    patch_w3_and_explorer: None,
) -> DelegateContracts:
    scripted_rp.set_address("rocketMinipoolDelegate", LATEST_MINIPOOL)
    scripted_rp.set_address("rocketMegapoolDelegate", LATEST_MEGAPOOL)
    return DelegateContracts(make_bot(db=mongo_db))


class TestMinipoolDelegates:
    async def test_groups_by_effective_delegate_and_flags_latest(
        self,
        cog: DelegateContracts,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        await mongo_db.minipools.insert_many(
            [
                {
                    "effective_delegate": LATEST_MINIPOOL,
                    "use_latest_delegate": True,
                    "beacon": {"status": "active_ongoing"},
                },
                {
                    "effective_delegate": LATEST_MINIPOOL,
                    "use_latest_delegate": False,
                    "beacon": {"status": "active_ongoing"},
                },
                {
                    "effective_delegate": OLD_MINIPOOL,
                    "use_latest_delegate": False,
                    "beacon": {"status": "active_ongoing"},
                },
                # Filtered out: not an in-queue / active status.
                {
                    "effective_delegate": OLD_MINIPOOL,
                    "use_latest_delegate": False,
                    "beacon": {"status": "withdrawal_done"},
                },
            ]
        )

        interaction = make_interaction()
        embed = await run_command(cog, "minipool_delegates", interaction)

        assert embed.title == "Minipool Delegate Stats"
        assert embed.description is not None
        # Latest delegate is annotated.
        assert "(Latest)" in embed.description
        # The withdrawal-done row was filtered out → only 3 minipools counted.
        # 2 on latest = 66.67%, 1 on old = 33.33%.
        assert "66.67%" in embed.description
        assert "33.33%" in embed.description
        # use_latest tally: 1 Yes, 2 No.
        assert "**Yes**: 1 (33.33%)" in embed.description
        assert "**No**: 2 (66.67%)" in embed.description

    async def test_status_filter_excludes_exited_minipools(
        self,
        cog: DelegateContracts,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        # Only `pending_initialized`, `pending_queued`, and `active_ongoing`
        # should count; anything else drops out of the aggregation.
        await mongo_db.minipools.insert_many(
            [
                {
                    "effective_delegate": LATEST_MINIPOOL,
                    "use_latest_delegate": True,
                    "beacon": {"status": "active_exiting"},
                },
                {
                    "effective_delegate": LATEST_MINIPOOL,
                    "use_latest_delegate": True,
                    "beacon": {"status": "pending_queued"},
                },
            ]
        )

        interaction = make_interaction()
        embed = await run_command(cog, "minipool_delegates", interaction)
        # Only 1 of the 2 inserted rows passes the filter → 100%.
        assert embed.description is not None
        assert "100.00%" in embed.description


class TestMegapoolDelegates:
    async def test_groups_by_effective_delegate_under_nested_field(
        self,
        cog: DelegateContracts,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        # The megapool variant nests delegate fields under `megapool.*` and
        # filters on `megapool.active_validator_count > 0`. Inactive operators
        # (zero validators) must drop out.
        await mongo_db.node_operators.insert_many(
            [
                {
                    "megapool": {
                        "effective_delegate": LATEST_MEGAPOOL,
                        "use_latest_delegate": True,
                        "active_validator_count": 3,
                    }
                },
                {
                    "megapool": {
                        "effective_delegate": OLD_MEGAPOOL,
                        "use_latest_delegate": False,
                        "active_validator_count": 1,
                    }
                },
                # Should be excluded by the active_validator_count filter.
                {
                    "megapool": {
                        "effective_delegate": OLD_MEGAPOOL,
                        "use_latest_delegate": False,
                        "active_validator_count": 0,
                    }
                },
            ]
        )

        interaction = make_interaction()
        embed = await run_command(cog, "megapool_delegates", interaction)

        assert embed.title == "Megapool Delegate Stats"
        assert embed.description is not None
        assert "(Latest)" in embed.description
        assert "**Yes**: 1 (50.00%)" in embed.description
        assert "**No**: 1 (50.00%)" in embed.description


class TestZeroDataEdgeCase:
    async def test_no_minipools_propagates_zero_division(
        self,
        cog: DelegateContracts,
        mongo_db: AsyncDatabase[dict[str, Any]],
    ) -> None:
        # Empty collection → aggregation produces no rows → percentage math
        # hits a ZeroDivisionError. Pin this so we know if/when the cog grows
        # an empty-state guard.
        interaction = make_interaction()
        interaction.followup.send = AsyncMock()
        with pytest.raises(ZeroDivisionError):
            await cog.minipool_delegates.callback(cog, interaction)
