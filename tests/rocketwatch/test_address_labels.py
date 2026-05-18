from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_typing import ChecksumAddress
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.utils import address_labels as al_module
from rocketwatch.utils.address_labels import (
    _PROXY_CLASSES,
    _format_project,
    _most_attested,
    _pick_display_name,
    get_address_name,
)


def _lbl(tag_id: str, tag_value: str) -> dict:
    return {"tag_id": tag_id, "tag_value": tag_value}


class TestMostAttested:
    def test_single_entry(self):
        assert _most_attested([_lbl("contract_name", "Foo")]) == "Foo"

    def test_picks_majority(self):
        entries = [
            _lbl("contract_name", "Foo"),
            _lbl("contract_name", "Foo"),
            _lbl("contract_name", "Bar"),
        ]
        assert _most_attested(entries) == "Foo"

    def test_tie_returns_one_of_the_values(self):
        # Counter ordering on ties is implementation-defined; just assert it's a valid pick.
        entries = [
            _lbl("contract_name", "Foo"),
            _lbl("contract_name", "Bar"),
        ]
        assert _most_attested(entries) in {"Foo", "Bar"}


class TestFormatProject:
    def test_passthrough_titlecases(self):
        assert _format_project("lido") == "Lido"

    def test_known_override_circle(self):
        assert _format_project("circlefin") == "Circle"

    def test_known_override_tether(self):
        assert _format_project("tetherto") == "Tether"

    def test_multiword_slug(self):
        # Title() turns it into capitalized-words, no override needed.
        assert _format_project("rocketpool") == "Rocketpool"


class TestPickDisplayName:
    def test_empty_returns_none(self):
        assert _pick_display_name([]) is None

    def test_owner_project_only(self):
        labels = [_lbl("owner_project", "lido")]
        assert _pick_display_name(labels) == "Lido"

    def test_erc20_name_takes_precedence_over_contract_name(self):
        labels = [
            _lbl("erc20.name", "USD Coin"),
            _lbl("contract_name", "FiatTokenV2_2"),
        ]
        assert _pick_display_name(labels) == "USD Coin"

    def test_erc20_beats_erc721_when_both_present(self):
        # Loop order is erc20 → erc721 → erc1155; erc20 wins.
        labels = [
            _lbl("erc721.name", "MyNFT"),
            _lbl("erc20.name", "MyToken"),
        ]
        assert _pick_display_name(labels) == "MyToken"

    def test_proxy_classes_skipped_in_contract_name(self):
        labels = [
            _lbl("contract_name", "TransparentUpgradeableProxy"),
            _lbl("contract_name", "RocketStorage"),
        ]
        assert _pick_display_name(labels) == "RocketStorage"

    def test_all_proxies_falls_back_to_proxy(self):
        # If every contract_name is a proxy class, we still pick one rather than returning None.
        labels = [_lbl("contract_name", "ERC1967Proxy")]
        assert _pick_display_name(labels) == "ERC1967Proxy"

    def test_project_and_specific_combined(self):
        labels = [
            _lbl("owner_project", "rocketpool"),
            _lbl("contract_name", "RocketDepositPool"),
        ]
        assert _pick_display_name(labels) == "Rocketpool: RocketDepositPool"

    def test_project_substring_of_specific_dedupes(self):
        # If the project name is already contained in the specific name, don't repeat it.
        labels = [
            _lbl("owner_project", "lido"),
            _lbl("contract_name", "LidoStakedETH"),
        ]
        assert _pick_display_name(labels) == "LidoStakedETH"

    def test_only_project_when_no_specific(self):
        assert _pick_display_name([_lbl("owner_project", "lido")]) == "Lido"

    def test_unknown_tags_ignored(self):
        labels = [_lbl("random_tag", "ignored")]
        assert _pick_display_name(labels) is None

    def test_erc1155_used_when_no_erc20_or_erc721(self):
        labels = [_lbl("erc1155.name", "MultiToken")]
        assert _pick_display_name(labels) == "MultiToken"


class TestProxyClasses:
    def test_known_proxy_names_present(self):
        # If someone renames the constants, these tests will catch it.
        assert "TransparentUpgradeableProxy" in _PROXY_CLASSES
        assert "ERC1967Proxy" in _PROXY_CLASSES
        assert "Proxy" in _PROXY_CLASSES


# ---- get_address_name: full resolution chain ------------------------------------

pytestmark = pytest.mark.integration_db

ADDR_MANUAL = "0x" + "AA" * 20
ADDR_CACHED = "0x" + "BB" * 20
ADDR_FRESH = "0x" + "CC" * 20


@pytest.fixture
def patch_collection(
    monkeypatch: pytest.MonkeyPatch,
    mongo_db: AsyncDatabase[dict[str, Any]],
) -> None:
    # Route the module-level cached `_collection` to our per-test mongo.
    monkeypatch.setattr(al_module, "_get_collection", lambda: mongo_db.address_labels)


@pytest.fixture
def manual_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # `_manual_names()` reads addresses.json and runs every entry through
    # `w3.to_checksum_address`. Skip the I/O — just install one known entry.
    monkeypatch.setattr(
        al_module,
        "_manual_names",
        lambda: {ChecksumAddress(ADDR_MANUAL): "Manual Override"},
    )


class TestGetAddressName:
    async def test_manual_override_short_circuits(
        self,
        manual_override: None,
        patch_collection: None,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If a Mongo lookup runs at all, this test should fail because we
        # never inserted a row for ADDR_MANUAL. Pin the contract: manual
        # entries win without ever touching mongo/OLI.
        explosive = AsyncMock(side_effect=AssertionError("OLI should not be called"))
        monkeypatch.setattr(al_module, "_fetch_from_oli", explosive)

        assert await get_address_name(ChecksumAddress(ADDR_MANUAL)) == "Manual Override"
        explosive.assert_not_awaited()

    async def test_mongo_cached_positive_hit(
        self,
        manual_override: None,
        patch_collection: None,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pre-seed mongo with a cached name. Get hit, OLI never called.
        await mongo_db.address_labels.insert_one(
            {"_id": ADDR_CACHED, "name": "Cached Label"}
        )
        explosive = AsyncMock(side_effect=AssertionError("OLI should not be called"))
        monkeypatch.setattr(al_module, "_fetch_from_oli", explosive)

        assert await get_address_name(ChecksumAddress(ADDR_CACHED)) == "Cached Label"

    async def test_mongo_cached_negative_within_ttl_returns_none(
        self,
        manual_override: None,
        patch_collection: None,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A cache entry with `name=None` plus a recent `fetched_at` means
        # "we asked OLI lately and it had nothing" — should return None
        # without re-fetching.
        await mongo_db.address_labels.insert_one(
            {
                "_id": ADDR_CACHED,
                "name": None,
                "fetched_at": datetime.now(UTC) - timedelta(days=1),
            }
        )
        explosive = AsyncMock(side_effect=AssertionError("OLI should not be called"))
        monkeypatch.setattr(al_module, "_fetch_from_oli", explosive)

        assert await get_address_name(ChecksumAddress(ADDR_CACHED)) is None

    async def test_mongo_negative_outside_ttl_refetches_from_oli(
        self,
        manual_override: None,
        patch_collection: None,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Stale negative cache (fetched > 7 days ago) — must refetch from OLI
        # and upsert the fresh result.
        await mongo_db.address_labels.insert_one(
            {
                "_id": ADDR_CACHED,
                "name": None,
                "fetched_at": datetime.now(UTC) - timedelta(days=30),
            }
        )
        monkeypatch.setattr(
            al_module, "_fetch_from_oli", AsyncMock(return_value="Fresh Name")
        )

        result = await get_address_name(ChecksumAddress(ADDR_CACHED))
        assert result == "Fresh Name"
        # And the cache should be upserted.
        cached = await mongo_db.address_labels.find_one({"_id": ADDR_CACHED})
        assert cached is not None
        assert cached["name"] == "Fresh Name"

    async def test_missing_entry_fetches_and_caches(
        self,
        manual_override: None,
        patch_collection: None,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No manual entry, no mongo cache → OLI fetch + upsert.
        monkeypatch.setattr(
            al_module, "_fetch_from_oli", AsyncMock(return_value="Looked Up")
        )
        result = await get_address_name(ChecksumAddress(ADDR_FRESH))
        assert result == "Looked Up"
        cached = await mongo_db.address_labels.find_one({"_id": ADDR_FRESH})
        assert cached is not None
        assert cached["name"] == "Looked Up"
        # fetched_at should also be present and recent.
        assert (datetime.now(UTC) - cached["fetched_at"]) < timedelta(seconds=60)

    async def test_oli_failure_returns_none_without_caching(
        self,
        manual_override: None,
        patch_collection: None,
        mongo_db: AsyncDatabase[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An OLI transport failure must NOT poison the cache — the next call
        # should be free to try again.
        monkeypatch.setattr(
            al_module,
            "_fetch_from_oli",
            AsyncMock(side_effect=RuntimeError("HTTP 500")),
        )
        assert await get_address_name(ChecksumAddress(ADDR_FRESH)) is None
        assert await mongo_db.address_labels.find_one({"_id": ADDR_FRESH}) is None

    async def test_mongo_lookup_failure_falls_through_to_oli(
        self,
        manual_override: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If the mongo lookup itself raises (network/connection issue), the
        # function should still try OLI and return whatever it returns.
        bad_collection = MagicMock()
        bad_collection.find_one = AsyncMock(
            side_effect=RuntimeError("mongo unreachable")
        )
        # We DO want the subsequent upsert to also fail gracefully.
        bad_collection.update_one = AsyncMock(
            side_effect=RuntimeError("mongo unreachable")
        )
        monkeypatch.setattr(al_module, "_get_collection", lambda: bad_collection)
        monkeypatch.setattr(
            al_module, "_fetch_from_oli", AsyncMock(return_value="Resilient")
        )

        assert await get_address_name(ChecksumAddress(ADDR_FRESH)) == "Resilient"
