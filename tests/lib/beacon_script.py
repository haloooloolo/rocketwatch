"""Scripted beacon node for plugin tests.

`ScriptedBeacon` mimics the `Bacon` proxy's method surface and returns canned
responses. Wire it up via the `scripted_bacon` fixture, which swaps it into
`shared_w3.bacon._instance`.

The shape of each response mirrors a real beacon node so tests exercise the
real parsing path in consumer code (string-encoded numbers, nested `data`
envelopes, etc.).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any


def make_validator_record(
    *,
    pubkey: str,
    index: int,
    status: str = "active_ongoing",
    balance_gwei: int = 32_000_000_000,
    effective_balance_gwei: int = 32_000_000_000,
    slashed: bool = False,
    activation_eligibility_epoch: int = 0,
    activation_epoch: int = 0,
    exit_epoch: int = 2**32 - 1,
    withdrawable_epoch: int = 2**32 - 1,
) -> dict[str, Any]:
    """Build the per-validator record returned by `get_validators_by_ids`.

    Numeric fields are stringified to match real beacon node JSON.
    """
    return {
        "index": str(index),
        "balance": str(balance_gwei),
        "status": status,
        "validator": {
            "pubkey": pubkey,
            "effective_balance": str(effective_balance_gwei),
            "slashed": slashed,
            "activation_eligibility_epoch": str(activation_eligibility_epoch),
            "activation_epoch": str(activation_epoch),
            "exit_epoch": str(exit_epoch),
            "withdrawable_epoch": str(withdrawable_epoch),
        },
    }


class ScriptedBeacon:
    def __init__(self) -> None:
        self._validators_by_pubkey: dict[str, dict[str, Any]] = {}
        self._validators_by_index: dict[int, dict[str, Any]] = {}
        self._blocks: dict[str, dict[str, Any] | BaseException] = {}
        self._block_headers: dict[str, dict[str, Any] | BaseException] = {}
        self._sync_committees: dict[int, dict[str, Any]] = {}
        self._proposer_duties: dict[str, list[dict[str, Any]] | BaseException] = {}
        self._finality_checkpoints: dict[str, dict[str, Any] | BaseException] = {}

    def register_validator(self, record: dict[str, Any]) -> None:
        """Index a validator record so it's returned by both lookup APIs."""
        pubkey = record["validator"]["pubkey"]
        index = int(record["index"])
        self._validators_by_pubkey[pubkey] = record
        self._validators_by_index[index] = record

    def register_validators(self, records: Iterable[dict[str, Any]]) -> None:
        for r in records:
            self.register_validator(r)

    def set_block(
        self, slot_or_state: str, message: dict[str, Any] | BaseException
    ) -> None:
        """Script `get_block(slot_or_state)`. Pass an exception to raise."""
        self._blocks[slot_or_state] = message

    def set_block_header(
        self, slot_or_state: str, header: dict[str, Any] | BaseException
    ) -> None:
        self._block_headers[slot_or_state] = header

    def set_sync_committee(self, epoch: int, data: dict[str, Any]) -> None:
        self._sync_committees[epoch] = data

    def set_proposer_duties(
        self, epoch: str, duties: list[dict[str, Any]] | BaseException
    ) -> None:
        self._proposer_duties[epoch] = duties

    def set_finality_checkpoint(
        self, slot_or_state: str, checkpoint: dict[str, Any] | BaseException
    ) -> None:
        self._finality_checkpoints[slot_or_state] = checkpoint

    async def get_validator(self, target: str | int) -> dict[str, Any]:
        if isinstance(target, int) and target in self._validators_by_index:
            return {"data": self._validators_by_index[target]}
        key = str(target)
        if key in self._validators_by_pubkey:
            return {"data": self._validators_by_pubkey[key]}
        try:
            idx = int(key)
        except ValueError:
            pass
        else:
            if idx in self._validators_by_index:
                return {"data": self._validators_by_index[idx]}
        raise KeyError(f"No scripted validator for {target!r}")

    async def get_validators_by_ids(
        self, state_id: str, ids: Sequence[str | int]
    ) -> dict[str, Any]:
        del state_id  # the fake doesn't model historical state
        data: list[dict[str, Any]] = []
        for raw in ids:
            if isinstance(raw, int):
                rec = self._validators_by_index.get(raw)
            else:
                rec = self._validators_by_pubkey.get(raw)
                if rec is None:
                    try:
                        rec = self._validators_by_index.get(int(raw))
                    except ValueError:
                        rec = None
            if rec is not None:
                data.append(rec)
        return {"data": data}

    async def get_block(self, slot_or_state: str) -> dict[str, Any]:
        if slot_or_state not in self._blocks:
            raise KeyError(f"No scripted block for {slot_or_state!r}")
        result = self._blocks[slot_or_state]
        if isinstance(result, BaseException):
            raise result
        return {"data": {"message": result}}

    async def get_block_header(self, slot_or_state: str) -> dict[str, Any]:
        if slot_or_state not in self._block_headers:
            raise KeyError(f"No scripted block header for {slot_or_state!r}")
        result = self._block_headers[slot_or_state]
        if isinstance(result, BaseException):
            raise result
        return {"data": {"header": {"message": result}}}

    async def get_sync_committee(self, epoch: int) -> dict[str, Any]:
        if epoch not in self._sync_committees:
            raise KeyError(f"No scripted sync committee for epoch {epoch}")
        return {"data": self._sync_committees[epoch]}

    async def get_block_proposer_duties(self, epoch: str) -> dict[str, Any]:
        if epoch not in self._proposer_duties:
            raise KeyError(f"No scripted proposer duties for epoch {epoch!r}")
        result = self._proposer_duties[epoch]
        if isinstance(result, BaseException):
            raise result
        return {"data": result}

    async def get_finality_checkpoint(self, slot_or_state: str) -> dict[str, Any]:
        if slot_or_state not in self._finality_checkpoints:
            raise KeyError(f"No scripted finality checkpoint for {slot_or_state!r}")
        result = self._finality_checkpoints[slot_or_state]
        if isinstance(result, BaseException):
            raise result
        return {"data": result}
