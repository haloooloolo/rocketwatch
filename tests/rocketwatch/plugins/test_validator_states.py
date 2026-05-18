from collections.abc import Iterator
from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.validator_states.validator_states import (
    ValidatorStates,
    _classify_beacon_validator,
    _classify_collection,
    _collapse_tree,
)
from tests.lib.discord_harness import (
    captured_embed,
    make_bot,
    make_interaction,
    run_command,
)


@pytest.fixture(autouse=True)
def _stub_explorer(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def fake_el(target: str, *, prefix: str = "", name: str | None = None) -> str:
        return f"[{prefix}{target}](el/{target})"

    monkeypatch.setattr(
        "rocketwatch.plugins.validator_states.validator_states.el_explorer_url",
        fake_el,
    )
    monkeypatch.setattr(
        "rocketwatch.plugins.validator_states.validator_states.w3.to_checksum_address",
        lambda a: a,
    )
    yield


class TestClassifyBeaconValidator:
    @pytest.mark.parametrize(
        ("status", "contract_status", "expected"),
        [
            ("pending_initialized", "in_queue", ("pending", "unassigned")),
            ("pending_initialized", "prestaked", ("pending", "prestaked")),
            ("pending_initialized", "staking", ("pending", "staked")),
            ("pending_initialized", "dissolved", ("dissolved", None)),
            ("pending_queued", "in_queue", ("pending", "queued")),
            ("active_ongoing", "staking", ("active", "ongoing")),
            ("active_exiting", "staking", ("exiting", "voluntarily")),
            ("active_slashed", "staking", ("exiting", "slashed")),
        ],
    )
    def test_known_status_mappings(
        self, status: str, contract_status: str, expected: tuple[str, str | None]
    ) -> None:
        assert (
            _classify_beacon_validator(
                {"status": status, "slashed": False}, contract_status
            )
            == expected
        )

    def test_exited_unslashed_picks_voluntarily(self) -> None:
        cat, sub = _classify_beacon_validator(
            {"status": "exited_unslashed", "slashed": False}, "staking"
        )
        assert (cat, sub) == ("exited", "voluntarily")

    def test_exited_slashed_picks_slashed(self) -> None:
        cat, sub = _classify_beacon_validator(
            {"status": "exited_slashed", "slashed": True}, "staking"
        )
        assert (cat, sub) == ("exited", "slashed")

    def test_withdrawal_done_unslashed(self) -> None:
        cat, sub = _classify_beacon_validator(
            {"status": "withdrawal_done", "slashed": False}, "staking"
        )
        assert (cat, sub) == ("withdrawn", "unslashed")

    def test_unknown_status_returns_none(self) -> None:
        cat, sub = _classify_beacon_validator(
            {"status": "bogus_state", "slashed": False}, "staking"
        )
        assert (cat, sub) == (None, None)


class TestClassifyCollection:
    def test_buckets_by_category(self) -> None:
        docs = [
            {
                "beacon": {"status": "active_ongoing", "slashed": False},
                "status": "staking",
            },
            {
                "beacon": {"status": "active_ongoing", "slashed": False},
                "status": "staking",
            },
            {
                "beacon": {"status": "active_exiting", "slashed": False},
                "status": "staking",
            },
            {
                "beacon": {"status": "withdrawal_done", "slashed": False},
                "status": "staking",
            },
        ]
        data, exiting, withdrawn = _classify_collection(docs, lambda d: False)
        assert data["active"] == {"ongoing": 2}
        assert data["exiting"] == {"voluntarily": 1}
        assert data["withdrawn"] == {"unslashed": 1}
        assert len(exiting) == 1
        assert len(withdrawn) == 1

    def test_done_fn_promotes_withdrawn_to_closed(self) -> None:
        docs = [
            {
                "beacon": {"status": "withdrawal_done", "slashed": False},
                "status": "exited",
            },
        ]
        data, _exiting, withdrawn = _classify_collection(
            docs, lambda d: d.get("status") == "exited"
        )
        # The "closed" bucket inherits the slashed/unslashed sub-category.
        assert data["closed"] == {"unslashed": 1}
        assert data["withdrawn"] == {}
        # Once promoted to "closed" the doc drops out of the withdrawn list.
        assert withdrawn == []

    def test_docs_without_beacon_still_classified_by_contract_status(self) -> None:
        docs: list[dict[str, Any]] = [
            {"status": "prestaked"},
            {"status": "in_queue"},
            {"status": "dissolved"},
            {"status": "unrecognized"},  # fall-through: no bucket
        ]
        data, _, _ = _classify_collection(docs, lambda d: False)
        assert data["pending"] == {"prestaked": 1, "unassigned": 1}
        assert data["dissolved"] == 1


class TestCollapseTree:
    def test_single_subcategory_collapses_to_count(self) -> None:
        collapsed = _collapse_tree(
            {"active": {"ongoing": 5}, "pending": {"a": 1, "b": 2}}
        )
        assert collapsed["active"] == 5
        assert collapsed["pending"] == {"a": 1, "b": 2}

    def test_int_values_pass_through(self) -> None:
        collapsed = _collapse_tree({"dissolved": 3})
        assert collapsed["dissolved"] == 3


class TestValidatorStatesCommand:
    async def test_renders_tree_when_no_listings(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.minipools.insert_many(
            [
                {
                    "beacon": {"status": "active_ongoing", "slashed": False},
                    "status": "staking",
                    "node_operator": "0x" + "1" * 40,
                    "validator_index": 1,
                },
                {
                    "beacon": {"status": "active_ongoing", "slashed": False},
                    "status": "staking",
                    "node_operator": "0x" + "1" * 40,
                    "validator_index": 2,
                },
            ]
        )
        cog = ValidatorStates(make_bot(db=mongo_db))
        embed = await run_command(cog, "validator_states", make_interaction())
        assert embed.title == "Validator States"
        assert embed.description is not None
        # render_tree_legacy uppercases bucket labels via .title().
        assert "Active:" in embed.description
        assert "Validators:" in embed.description

    async def test_few_exiting_validators_inline_listed(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.minipools.insert_one(
            {
                "beacon": {"status": "active_exiting", "slashed": False},
                "status": "staking",
                "node_operator": "0x" + "a" * 40,
                "validator_index": 9876,
            }
        )
        cog = ValidatorStates(make_bot(db=mongo_db))
        embed = await run_command(cog, "validator_states", make_interaction())
        assert embed.description is not None
        assert "Exiting Validators" in embed.description
        assert "9876" in embed.description

    async def test_many_validators_aggregated_by_node_operator(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        # 24+ exiting/withdrawn validators triggers the node-operator-grouped path.
        docs = [
            {
                "beacon": {"status": "active_exiting", "slashed": False},
                "status": "staking",
                "node_operator": f"0x{i:040d}",
                "validator_index": i,
            }
            for i in range(30)
        ]
        await mongo_db.minipools.insert_many(docs)
        cog = ValidatorStates(make_bot(db=mongo_db))
        await run_command(cog, "validator_states", make_interaction())
        # Confirm the aggregated header is in the description.
        interaction = make_interaction()
        await cog.validator_states.callback(cog, interaction)
        embed = captured_embed(interaction)
        assert embed.description is not None
        assert "Exiting Node Operators" in embed.description
