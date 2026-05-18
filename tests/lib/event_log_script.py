"""Scripted `w3.eth.get_logs` for plugin-level event tests.

`EventLogScript` holds a list of canned `LogReceipt`s; its `.get_logs(filter)`
method returns those that match the filter's `address` / `topics` / `fromBlock`
/ `toBlock` constraints — the same shape as `w3.eth.get_logs`. Wire it up by
patching `w3.eth.get_logs = script.get_logs`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from eth_typing import BlockNumber, ChecksumAddress
from hexbytes import HexBytes
from web3.types import LogReceipt


def _to_hexbytes(value: bytes | str) -> HexBytes:
    return HexBytes(value)


def make_log(
    *,
    address: ChecksumAddress | str,
    topics: Iterable[bytes | str],
    block_number: int,
    data: bytes | str = b"",
    log_index: int = 0,
    transaction_index: int = 0,
    transaction_hash: bytes | str = b"\x00" * 32,
    block_hash: bytes | str = b"\x00" * 32,
    removed: bool = False,
) -> LogReceipt:
    """Construct a `LogReceipt` from convenient Python literals."""
    return LogReceipt(  # type: ignore[typeddict-item]
        address=address,  # type: ignore[arg-type]
        topics=[_to_hexbytes(t) for t in topics],
        data=_to_hexbytes(data),
        blockNumber=BlockNumber(block_number),
        blockHash=_to_hexbytes(block_hash),
        transactionHash=_to_hexbytes(transaction_hash),
        transactionIndex=transaction_index,
        logIndex=log_index,
        removed=removed,
    )


class EventLogScript:
    """Scripted store for `w3.eth.get_logs`."""

    def __init__(self) -> None:
        self._logs: list[LogReceipt] = []

    def add(self, log: LogReceipt) -> None:
        self._logs.append(log)

    def add_many(self, logs: Iterable[LogReceipt]) -> None:
        self._logs.extend(logs)

    def clear(self) -> None:
        self._logs.clear()

    async def get_logs(self, filter_params: dict[str, Any]) -> list[LogReceipt]:
        """Return scripted logs that match the filter's constraints."""
        return [log for log in self._logs if self._matches(log, filter_params)]

    @staticmethod
    def _matches(log: LogReceipt, params: dict[str, Any]) -> bool:
        # Address filter — either a single address or a list of allowed ones.
        if "address" in params:
            allowed = params["address"]
            if isinstance(allowed, str):
                allowed = [allowed]
            if log["address"] not in {addr.lower() for addr in allowed} and log[
                "address"
            ] not in set(allowed):
                return False

        # Block range — fromBlock / toBlock can be ints or "latest".
        if (
            "fromBlock" in params
            and isinstance(params["fromBlock"], int)
            and int(log["blockNumber"]) < params["fromBlock"]
        ):
            return False
        if (
            "toBlock" in params
            and isinstance(params["toBlock"], int)
            and int(log["blockNumber"]) > params["toBlock"]
        ):
            return False

        # Topic filter — `topics` is a list of slots; each slot can be a
        # single topic or a list of allowed topics. None or absent = wildcard.
        if "topics" in params:
            for i, slot in enumerate(params["topics"]):
                if slot is None:
                    continue
                if i >= len(log["topics"]):
                    return False
                actual = HexBytes(log["topics"][i])
                allowed_topics: Sequence[Any] = (
                    slot if isinstance(slot, list) else [slot]
                )
                if not any(HexBytes(t) == actual for t in allowed_topics):
                    return False

        return True
