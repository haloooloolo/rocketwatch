from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any, cast

import aiohttp
from eth_typing import ChecksumAddress
from pymongo import AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection

from rocketwatch.utils.config import cfg
from rocketwatch.utils.shared_w3 import w3

log = logging.getLogger("rocketwatch.address_labels")

_OLI_URL = "https://api.openlabelsinitiative.org/labels"
_CHAIN_ID = "eip155:1"
_NEGATIVE_TTL = timedelta(days=7)
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

_MANUAL_PATH = Path(__file__).parent.parent / "resources" / "addresses.json"


@cache
def _manual_names() -> dict[ChecksumAddress, str]:
    return {
        w3.to_checksum_address(addr): name
        for addr, name in json.loads(_MANUAL_PATH.read_text()).items()
    }


# Common proxy class names — skip when picking from contract_name candidates.
_PROXY_CLASSES = frozenset(
    {
        "AppProxyUpgradeable",
        "TransparentUpgradeableProxy",
        "ERC1967Proxy",
        "BeaconProxy",
        "Proxy",
        "InitializableProxy",
        "InitializableImmutableAdminUpgradeabilityProxy",
    }
)

# OLI project slugs whose .title() casing reads poorly. Grow as needed.
_PROJECT_OVERRIDES: dict[str, str] = {
    "circlefin": "Circle",
    "tetherto": "Tether",
}


_session: aiohttp.ClientSession | None = None
_collection: AsyncCollection[dict[str, Any]] | None = None


def _get_collection() -> AsyncCollection[dict[str, Any]]:
    global _collection
    if _collection is None:
        client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            cfg.mongodb.uri, tz_aware=True
        )
        _collection = client.rocketwatch.address_labels
    return _collection


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            headers={"X-API-Key": cfg.secrets.openlabels},
            timeout=_REQUEST_TIMEOUT,
        )
    return _session


def _most_attested(entries: list[dict[str, Any]]) -> str:
    return cast(str, Counter(e["tag_value"] for e in entries).most_common(1)[0][0])


def _format_project(slug: str) -> str:
    return _PROJECT_OVERRIDES.get(slug, slug.title())


def _pick_display_name(labels: list[dict[str, Any]]) -> str | None:
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for lbl in labels:
        by_tag.setdefault(lbl["tag_id"], []).append(lbl)

    project: str | None = None
    if proj_entries := by_tag.get("owner_project"):
        project = _format_project(_most_attested(proj_entries))

    specific: str | None = None
    for tag in ("erc20.name", "erc721.name", "erc1155.name"):
        if entries := by_tag.get(tag):
            specific = _most_attested(entries)
            break
    if specific is None and (contracts := by_tag.get("contract_name")):
        non_proxy = [c for c in contracts if c["tag_value"] not in _PROXY_CLASSES]
        specific = _most_attested(non_proxy or contracts)

    if specific and project:
        if project.lower() in specific.lower():
            return specific
        return f"{project}: {specific}"
    return specific or project


async def _fetch_from_oli(address: ChecksumAddress) -> str | None:
    """Query OLI for `address` and return the picked display name (or None).

    Raises on transport errors so the caller can choose not to cache.
    """
    session = await _get_session()
    async with session.get(
        _OLI_URL, params={"address": address, "chain_id": _CHAIN_ID}
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"OLI returned HTTP {resp.status}")
        data = await resp.json()
    return _pick_display_name(data.get("labels", []))


async def get_address_name(address: ChecksumAddress) -> str | None:
    """Look up a display name for `address`.

    Resolution order: hand-curated overrides (resources/addresses.json) →
    MongoDB cache → OLI. Returns None when no label exists or transport
    failures prevent a result.
    """
    if manual := _manual_names().get(address):
        return manual

    collection = _get_collection()

    try:
        cached = await collection.find_one({"_id": address})
    except Exception:
        log.exception("Mongo lookup failed for %s; falling through to OLI", address)
        cached = None

    if cached is not None:
        cached_name = cached.get("name")
        if isinstance(cached_name, str):
            return cached_name
        fetched_at = cached.get("fetched_at")
        if isinstance(fetched_at, datetime) and (
            datetime.now(UTC) - fetched_at < _NEGATIVE_TTL
        ):
            return None

    try:
        name = await _fetch_from_oli(address)
    except Exception:
        log.exception("OLI fetch failed for %s; not caching", address)
        return None

    try:
        await collection.update_one(
            {"_id": address},
            {"$set": {"name": name, "fetched_at": datetime.now(UTC)}},
            upsert=True,
        )
    except Exception:
        log.exception("Mongo upsert failed for %s; result not cached", address)

    return name
