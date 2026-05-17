import bz2
import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, cast

_DATA_PATH = Path(__file__).parent.parent / "resources" / "etherscan_addresses.json.bz2"


@dataclass(frozen=True)
class Label:
    id: str


@dataclass(frozen=True)
class Address:
    name: str | None = None
    labels: list[Label] = field(default_factory=list)


@cache
def _load() -> dict[str, dict[str, Any]]:
    with bz2.open(_DATA_PATH, "rb") as f:
        return cast(dict[str, dict[str, Any]], json.load(f))


def get_address(address: str) -> Address:
    data = _load().get(address.lower(), {})
    labels = [Label(id=label["label_id"]) for label in data.get("labels", [])]
    return Address(name=data.get("name"), labels=labels)
