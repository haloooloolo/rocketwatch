from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from web3.types import EventData, LogReceipt

from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.shared_w3 import w3


@dataclass
class ConflictRule:
    """When `winner` exists in a tx, remove `loser` events."""

    winner: str
    loser: str
    match: Callable[[LogReceipt | EventData, LogReceipt | EventData], bool] | None = (
        None
    )


@dataclass
class AggregationRule:
    """Deduplicate events; store aggregated value on the surviving event."""

    event: str
    attribute: str
    method: Literal["count", "sum"]
    group_by: Callable[[LogReceipt | EventData], Any] | None = None


@dataclass
class DeduplicationRule:
    """Keep only the event with the best value of `attribute`."""

    event: str
    attribute: str
    keep: Literal["max", "min"] = "max"


CONFLICT_RULES = (
    ConflictRule("rocketTokenRETH.TokensBurned", "rocketTokenRETH.Transfer"),
    ConflictRule("rocketDepositPool.DepositReceived", "rocketTokenRETH.Transfer"),
    ConflictRule("MinipoolScrubbed", "StatusUpdated"),
    ConflictRule(
        "rocketDAOProtocolProposal.ProposalVoteOverridden",
        "rocketDAOProtocolProposal.ProposalVoted",
    ),
    ConflictRule(
        "MinipoolPrestaked",
        "rocketDepositPool.DepositAssigned",
        match=lambda winner, loser: (
            winner["address"]
            == w3.to_checksum_address(cast(LogReceipt, loser)["topics"][1][-20:])
        ),
    ),
)

AGGREGATION_RULES = (
    AggregationRule("rocketDepositPool.DepositAssigned", "assignmentCount", "count"),
    AggregationRule(
        "MegapoolValidatorAssigned",
        "assignmentCount",
        "count",
        group_by=lambda e: e["address"],
    ),
    AggregationRule("unstETH.WithdrawalRequested", "amountOfStETH", "sum"),
)

DEDUP_RULES = (DeduplicationRule("rocketTokenRETH.Transfer", "value", "max"),)


def get_event_name(
    event: LogReceipt | EventData,
    topic_map: dict[str, str],
) -> tuple[str, str]:
    if "topics" in event:
        receipt = cast(LogReceipt, event)
        contract_name = rp.get_name_by_address(receipt["address"])
        name = topic_map[receipt["topics"][0].hex()]
    else:
        contract_name = None
        name = event.get("event", "")

    full_name = f"{contract_name}.{name}" if contract_name else name
    return name, full_name


async def _decode_event_attr(
    event: LogReceipt | EventData,
    attribute: str,
    topic_map: dict[str, str],
) -> Any:
    name, _ = get_event_name(event, topic_map)
    contract = await rp.get_contract_by_address(event["address"])
    assert contract is not None
    decoded = dict(contract.events[name]().process_log(event))
    return decoded["args"][attribute]


async def aggregate_events(
    events: list[LogReceipt | EventData],
    topic_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Aggregate and deduplicate events within the same transaction.

    Returns plain dicts (copies of the original events) with any
    aggregation attributes merged in as top-level keys.
    """
    # Group events by transaction
    events_by_tx: dict[Any, list[LogReceipt | EventData]] = {}
    for event in reversed(events):
        tx_hash = event["transactionHash"]
        if tx_hash not in events_by_tx:
            events_by_tx[tx_hash] = []
        events_by_tx[tx_hash].append(event)

    # Track which events survive and any extra attributes to attach
    extra_attrs: defaultdict[int, dict[str, Any]] = defaultdict(dict)
    all_to_remove: set[int] = set()

    for tx_events in events_by_tx.values():
        # Build name lookup for this transaction
        by_name: dict[str, list[LogReceipt | EventData]] = {}
        for event in tx_events:
            _, full_name = get_event_name(event, topic_map)
            if full_name not in by_name:
                by_name[full_name] = []
            by_name[full_name].append(event)

        to_remove: set[int] = set()

        # Pass 1: Conflicts
        for rule in CONFLICT_RULES:
            winners = by_name.get(rule.winner, [])
            losers = by_name.get(rule.loser, [])
            if not winners or not losers:
                continue
            for loser in losers:
                if id(loser) in to_remove:
                    continue
                if rule.match is None:
                    to_remove.add(id(loser))
                else:
                    for winner in winners:
                        if rule.match(winner, loser):
                            to_remove.add(id(loser))
                            break

        # Pass 2: Deduplication (keep best)
        for dedup_rule in DEDUP_RULES:
            dupes = [
                e for e in by_name.get(dedup_rule.event, []) if id(e) not in to_remove
            ]
            if len(dupes) <= 1:
                continue
            best = dupes[0]
            best_val = await _decode_event_attr(best, dedup_rule.attribute, topic_map)
            for other in dupes[1:]:
                other_val = await _decode_event_attr(
                    other, dedup_rule.attribute, topic_map
                )
                if (dedup_rule.keep == "max" and other_val > best_val) or (
                    dedup_rule.keep == "min" and other_val < best_val
                ):
                    to_remove.add(id(best))
                    best, best_val = other, other_val
                else:
                    to_remove.add(id(other))

        # Pass 3: Aggregation
        for agg_rule in AGGREGATION_RULES:
            dupes = [
                e for e in by_name.get(agg_rule.event, []) if id(e) not in to_remove
            ]
            if not dupes:
                continue

            # Split into groups
            groups: dict[Any, list[LogReceipt | EventData]] = {}
            for dupe in dupes:
                key = agg_rule.group_by(dupe) if agg_rule.group_by else None
                if key not in groups:
                    groups[key] = []
                groups[key].append(dupe)

            for group_events in groups.values():
                agg_value = 0
                if agg_rule.method == "count":
                    agg_value = len(group_events)
                elif agg_rule.method == "sum":
                    for dupe in group_events:
                        agg_value += await _decode_event_attr(
                            dupe, agg_rule.attribute, topic_map
                        )
                extra_attrs[id(group_events[0])][agg_rule.attribute] = agg_value
                # Remove the rest
                for dupe in group_events[1:]:
                    to_remove.add(id(dupe))

        all_to_remove.update(to_remove)

    # Build result: plain dicts with aggregation attributes merged in
    return [
        {**event, **extra_attrs.get(id(event), {})}
        for event in events
        if id(event) not in all_to_remove
    ]
