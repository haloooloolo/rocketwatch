"""Declarative formatting markers for event args TypedDicts.

Provides ``Annotated`` type aliases (``Wei``, ``NodeAddress``, etc.) that
declare *how* a field should be formatted.  The ``auto_format`` function
reads those annotations at runtime and returns a new dict with converted
values.

Both the log-events and transactions modules import from here so that
marker definitions stay in one place.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, get_type_hints

from eth_typing import ChecksumAddress

from utils import solidity
from utils.embeds import el_explorer_url
from utils.sea_creatures import get_sea_creature_for_address

# ---------------------------------------------------------------------------
# Marker dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Wei:
    """Marker: int field → ``solidity.to_float(value, decimals)``."""

    decimals: int = 18


@dataclass(frozen=True, slots=True)
class _Percentage:
    """Marker: int field → ``100 * solidity.to_float(value, decimals)``."""

    decimals: int = 18


@dataclass(frozen=True, slots=True)
class _NodeAddress:
    """Marker: node operator address → ``_addr()`` (with sea creature)."""


@dataclass(frozen=True, slots=True)
class _ContractAddress:
    """Marker: smart contract address → ``_addr()``."""


@dataclass(frozen=True, slots=True)
class _WalletAddress:
    """Marker: generic wallet/EOA address → ``_addr()``."""


@dataclass(frozen=True, slots=True)
class _MinipoolAddress:
    """Marker: minipool address → ``_addr()``."""


@dataclass(frozen=True, slots=True)
class _MegapoolAddress:
    """Marker: megapool address → ``_addr()``."""


# ---------------------------------------------------------------------------
# Type aliases — use these in TypedDict field annotations
# ---------------------------------------------------------------------------

Wei = Annotated[int, _Wei()]
TWei = Annotated[int, _Wei(decimals=6)]
Percentage = Annotated[int, _Percentage()]
NodeAddress = Annotated[ChecksumAddress, _NodeAddress()]
ContractAddress = Annotated[ChecksumAddress, _ContractAddress()]
WalletAddress = Annotated[ChecksumAddress, _WalletAddress()]
MinipoolAddress = Annotated[ChecksumAddress, _MinipoolAddress()]
MegapoolAddress = Annotated[ChecksumAddress, _MegapoolAddress()]

_MARKER_TYPES = (
    _Wei,
    _Percentage,
    _NodeAddress,
    _ContractAddress,
    _WalletAddress,
    _MinipoolAddress,
    _MegapoolAddress,
)

_ADDR_MARKER_TYPES = (
    _NodeAddress,
    _ContractAddress,
    _WalletAddress,
    _MinipoolAddress,
    _MegapoolAddress,
)


# ---------------------------------------------------------------------------
# Marker introspection
# ---------------------------------------------------------------------------


def _get_marker(
    hint: Any,
) -> (
    _Wei
    | _Percentage
    | _NodeAddress
    | _ContractAddress
    | _WalletAddress
    | _MinipoolAddress
    | _MegapoolAddress
    | None
):
    """Extract the formatting marker from an ``Annotated`` type, if any."""
    if hasattr(hint, "__metadata__"):
        for m in hint.__metadata__:
            if isinstance(m, _MARKER_TYPES):
                return m
    return None


# ---------------------------------------------------------------------------
# Address formatting helpers
# ---------------------------------------------------------------------------


async def _addr(address: ChecksumAddress) -> str:
    """Address link with sea creature + role prefixes."""
    sea = await get_sea_creature_for_address(address)
    return str(await el_explorer_url(address, prefix=sea))


# ---------------------------------------------------------------------------
# Auto-format engine
# ---------------------------------------------------------------------------


async def auto_format(args: Mapping[str, Any], td_class: type) -> dict[str, Any]:
    """Format *args* based on ``Annotated`` markers on *td_class*.

    Returns a **new** dict with converted values.  Address fields are
    resolved concurrently via :func:`asyncio.gather`.
    """
    hints = get_type_hints(td_class, include_extras=True)
    result = dict(args)
    async_tasks: list[tuple[str, Any]] = []

    for key, hint in hints.items():
        if key not in args:
            continue
        marker = _get_marker(hint)
        if marker is None:
            continue
        raw = args[key]
        match marker:
            case _Wei(decimals=d):
                result[key] = solidity.to_float(raw, decimals=d)
            case _Percentage(decimals=d):
                result[key] = 100 * solidity.to_float(raw, decimals=d)
            case m if isinstance(m, _ADDR_MARKER_TYPES):
                async_tasks.append((key, _addr(raw)))

    if async_tasks:
        keys, coros = zip(*async_tasks, strict=True)
        values = await asyncio.gather(*coros)
        for k, v in zip(keys, values, strict=True):
            result[k] = v

    return result
