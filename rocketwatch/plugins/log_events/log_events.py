from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import warnings
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, Literal, cast

from discord import Interaction
from discord.app_commands import Choice, command, guilds
from discord.ext.commands import is_owner
from discord.ui import Modal, TextInput
from eth_typing.evm import BlockNumber, ChecksumAddress
from hexbytes import HexBytes
from web3.constants import HASH_ZERO
from web3.exceptions import BadFunctionCallOutput
from web3.logs import DISCARD
from web3.types import EventData, FilterParams, LogReceipt, TxReceipt

from rocketwatch import RocketWatch
from utils.config import cfg
from utils.event import Event, EventPlugin
from utils.rocketpool import NoAddressFound, rp
from utils.shared_w3 import w3

from .event_definitions import (
    EVENT_REGISTRY,
    LogEvent,
    LogEventContext,
    LogEventData,
)

log = logging.getLogger("rocketwatch.log_events")


def _get_event_fields(
    event_cls: LogEvent,
) -> list[tuple[str, bool]]:
    """Return ``[(name, required), ...]`` for non-context fields of *event_cls*'s Args."""
    args_type = type(event_cls).Args
    if args_type is LogEventContext:
        return []
    context_keys = set(LogEventContext.__annotations__)
    return [
        (name, name in args_type.__required_keys__)
        for name in args_type.__annotations__
        if name not in context_keys
    ]


class _PreviewLogModal(Modal):
    def __init__(
        self,
        event_cls: LogEvent,
        block_number: int,
        fields: list[tuple[str, bool]],
    ) -> None:
        super().__init__(title=event_cls.event_name[:45])
        self.event_cls = event_cls
        self.block_number = block_number
        self.fields = fields
        self.param_inputs: list[TextInput] = []
        for name, required in fields:
            text_input: TextInput = TextInput(label=name[:45], required=required)
            self.add_item(text_input)
            self.param_inputs.append(text_input)

    async def on_submit(self, interaction: Interaction) -> None:
        await interaction.response.defer()
        parsed_args: dict[str, Any] = {}
        for text_input, (name, _) in zip(self.param_inputs, self.fields, strict=True):
            if text_input.value:
                val: Any = text_input.value
                with contextlib.suppress(json.JSONDecodeError, ValueError):
                    val = json.loads(val)
                parsed_args[name] = val

        args: dict[str, Any] = {
            **parsed_args,
            "transactionHash": HASH_ZERO,
            "blockNumber": self.block_number,
            "event_name": self.event_cls.event_name,
        }
        event_data: LogEventData = {
            "hash": HASH_ZERO,
            "blockNumber": self.block_number,
        }
        resolved = await self.event_cls.resolve(args, event_data)
        if resolved is None:
            await interaction.followup.send(content="Event filtered out.")
            return
        embeds = await resolved.build_embeds(args, event_data, None)
        if embeds:
            await interaction.followup.send(embeds=embeds)
        else:
            await interaction.followup.send(content="No events triggered.")


PartialFilter = Callable[
    [BlockNumber, BlockNumber | Literal["latest"]],
    Coroutine[Any, Any, Sequence[LogReceipt | EventData]],
]

# Upgrade event names that trigger contract re-init
_UPGRADE_EVENTS: set[str] = {
    "odao_contract_upgraded_event",
    "odao_contract_added_event",
}


class LogEvents(EventPlugin):
    def __init__(self, bot: RocketWatch):
        super().__init__(bot)
        self._partial_filters: list[PartialFilter] = []
        # contract_name.SolidityEvent -> LogEvent handler
        self._event_map: dict[str, LogEvent] = {}
        # topic hex -> solidity event name
        self._topic_map: dict[str, str] = {}
        # solidity event name (no contract prefix) -> LogEvent handler (global only)
        self._global_event_map: dict[str, LogEvent] = {}

    async def async_init(self) -> None:
        (
            filters,
            event_map,
            topic_map,
            global_event_map,
        ) = await self._parse_event_config()
        self._partial_filters = filters
        self._event_map = event_map
        self._topic_map = topic_map
        self._global_event_map = global_event_map

    async def _parse_event_config(
        self,
    ) -> tuple[
        list[PartialFilter], dict[str, LogEvent], dict[str, str], dict[str, LogEvent]
    ]:
        event_map: dict[str, LogEvent] = {}
        topic_map: dict[str, str] = {}
        global_event_map: dict[str, LogEvent] = {}

        # Separate direct vs global based on is_global flag
        addresses: set[ChecksumAddress] = set()
        aggregated_topics: set[str] = set()

        global_topics: set[str] = set()
        global_topic_decoders: dict[str, type] = {}

        for contract_name, events in EVENT_REGISTRY.items():
            try:
                contract = await rp.get_contract_by_name(contract_name)
            except (NoAddressFound, Exception):
                log.warning("Failed to get contract %s", contract_name)
                continue

            for solidity_event_name, handler in events.items():
                try:
                    # Handle explicit ABI signatures like "RPLStaked(address,address,uint256,uint256)"
                    if "(" in solidity_event_name:
                        topic = w3.keccak(text=solidity_event_name).hex()
                        base_name = solidity_event_name.split("(")[0]
                    else:
                        event_abi = contract.events[solidity_event_name].abi
                        input_types = ",".join(
                            i["type"] for i in event_abi.get("inputs", [])
                        )
                        topic = w3.keccak(
                            text=f"{solidity_event_name}({input_types})"
                        ).hex()
                        base_name = solidity_event_name
                except Exception:
                    log.warning(
                        "Couldn't find event %s in contract %s",
                        solidity_event_name,
                        contract_name,
                    )
                    continue

                log.info("Adding filter for %s.%s", contract_name, solidity_event_name)

                if handler.is_global:
                    global_topics.add(topic)
                    global_topic_decoders[topic] = contract.events[solidity_event_name]
                    global_event_map[base_name] = handler
                else:
                    addresses.add(contract.address)
                    aggregated_topics.add(topic)
                    event_map[f"{contract_name}.{solidity_event_name}"] = handler
                    topic_map[topic] = solidity_event_name

        partial_filters: list[PartialFilter] = []

        if addresses:

            async def build_direct_filter(
                _from: BlockNumber, _to: BlockNumber | Literal["latest"]
            ) -> Sequence[LogReceipt | EventData]:
                return list(
                    await w3.eth.get_logs(
                        cast(
                            FilterParams,
                            {
                                "address": list(addresses),
                                "topics": [list(aggregated_topics)],
                                "fromBlock": _from,
                                "toBlock": _to,
                            },
                        )
                    )
                )

            partial_filters.append(build_direct_filter)

        if global_topics:

            async def build_global_filter(
                _from: BlockNumber, _to: BlockNumber | Literal["latest"]
            ) -> Sequence[LogReceipt | EventData]:
                raw_logs = await w3.eth.get_logs(
                    cast(
                        FilterParams,
                        {
                            "topics": [list(global_topics)],
                            "fromBlock": _from,
                            "toBlock": _to,
                        },
                    )
                )
                return [
                    global_topic_decoders[raw_log["topics"][0].hex()]().process_log(
                        raw_log
                    )
                    for raw_log in raw_logs
                ]

            partial_filters.append(build_global_filter)

        return partial_filters, event_map, topic_map, global_event_map

    # --- Slash commands ---

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def preview_log_event(
        self,
        interaction: Interaction,
        contract: str,
        event: str,
        block_number: int = 0,
    ) -> None:
        event_cls = EVENT_REGISTRY.get(contract, {}).get(event)
        if event_cls is None:
            await interaction.response.send_message(
                content="No event registered for that contract/event."
            )
            return

        fields = _get_event_fields(event_cls)
        if fields:
            modal = _PreviewLogModal(event_cls, block_number, fields)
            await interaction.response.send_modal(modal)
        else:
            await interaction.response.defer()
            args: dict[str, Any] = {
                "transactionHash": HASH_ZERO,
                "blockNumber": block_number,
                "event_name": event_cls.event_name,
            }
            event_data: LogEventData = {
                "hash": HASH_ZERO,
                "blockNumber": block_number,
            }
            resolved = await event_cls.resolve(args, event_data)
            if resolved is None:
                await interaction.followup.send(content="Event filtered out.")
                return
            embeds = await resolved.build_embeds(args, event_data, None)
            if embeds:
                await interaction.followup.send(embeds=embeds)
            else:
                await interaction.followup.send(content="No events triggered.")

    @preview_log_event.autocomplete("contract")
    async def _autocomplete_contract(
        self, interaction: Interaction, current: str
    ) -> list[Choice[str]]:
        return [
            Choice(name=name, value=name)
            for name in EVENT_REGISTRY
            if current.lower() in name.lower()
        ][:25]

    @preview_log_event.autocomplete("event")
    async def _autocomplete_event(
        self, interaction: Interaction, current: str
    ) -> list[Choice[str]]:
        contract = interaction.namespace.contract or ""
        events = EVENT_REGISTRY.get(contract, {})
        return [
            Choice(name=name, value=name)
            for name in events
            if current.lower() in name.lower()
        ][:25]

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def replay_log_events(self, interaction: Interaction, tx_hash: str) -> None:
        await interaction.response.defer()
        if not tx_hash.startswith("0x") or len(tx_hash) != 66:
            await interaction.followup.send(content="Invalid transaction hash.")
            return
        receipt: TxReceipt = await w3.eth.get_transaction_receipt(tx_hash)
        logs: list[dict[str, Any]] = receipt["logs"]  # type: ignore[assignment]

        filtered_events: list[dict[str, Any]] = []

        # get direct events
        for event_log in logs:
            topics = event_log.get("topics", [])
            if topics and (topics[0].hex() in self._topic_map):
                filtered_events.append(dict(event_log))

        # get global events
        for contract_name, events in EVENT_REGISTRY.items():
            has_global = any(h.is_global for h in events.values())
            if not has_global:
                continue
            try:
                contract = await rp.assemble_contract(name=contract_name)
            except Exception:
                continue
            for solidity_event_name, handler in events.items():
                if not handler.is_global:
                    continue
                base_name = solidity_event_name.split("(")[0]
                event_cls = contract.events[base_name]()
                rich_logs = event_cls.process_receipt(receipt, errors=DISCARD)
                filtered_events.extend(rich_logs)

        responses, _ = await self.process_events(filtered_events)
        if responses:
            await interaction.followup.send(
                embeds=[response.embed for response in responses]
            )
        else:
            await interaction.followup.send(content="No events found.")

    # --- EventPlugin lifecycle ---

    async def _get_new_events(self) -> list[Event]:
        from_block = self.last_served_block + 1 - self.lookback_distance
        return await self.get_past_events(from_block, self._pending_block)

    async def get_past_events(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[Event]:
        log.debug("Fetching events in [%s, %s]", from_block, to_block)
        log.debug("Using %d filters", len(self._partial_filters))

        events: list[dict[str, Any]] = []
        for pf in self._partial_filters:
            events.extend(await pf(from_block, to_block))  # type: ignore[arg-type]

        messages, contract_upgrade_block = await self.process_events(events)
        if not contract_upgrade_block:
            return messages

        log.info(
            "Detected contract upgrade at block %s, reinitializing",
            contract_upgrade_block,
        )
        old_config = (
            self._partial_filters,
            self._event_map,
            self._topic_map,
            self._global_event_map,
        )

        try:
            await rp.flush()
            await self.async_init()
            return messages + await self.get_past_events(
                BlockNumber(contract_upgrade_block + 1), to_block
            )
        except Exception as err:
            (
                self._partial_filters,
                self._event_map,
                self._topic_map,
                self._global_event_map,
            ) = old_config
            raise err

    # --- Event processing ---

    async def process_events(
        self, events: list[dict[str, Any]]
    ) -> tuple[list[Event], BlockNumber | None]:
        events.sort(key=lambda e: (e["blockNumber"], e["logIndex"]))
        messages: list[Event] = []
        upgrade_block: BlockNumber | None = None

        log.debug("Aggregating %d events", len(events))
        aggregated: list[dict[str, Any]] = await self.aggregate_events(events)
        log.debug("Processing %d events", len(aggregated))

        for event in aggregated:
            if event.get("removed", False):
                continue

            log.debug("Checking event %s", event)

            args_hash = hashlib.md5()

            def hash_args(_args: dict[str, Any], _hash: Any = args_hash) -> None:
                for k, v in sorted(_args.items()):
                    if not ("time" in k.lower() or "block" in k.lower()):
                        _hash.update(f"{k}:{v}".encode())

            event_cls: LogEvent | None = None

            contract_name = rp.get_name_by_address(event.get("address", ""))
            if contract_name and "topics" in event:
                # Direct event path
                log.debug("Found event %s for %s", event, contract_name)
                solidity_event_name = self._topic_map[event["topics"][0].hex()]
                key = f"{contract_name}.{solidity_event_name}"
                event_cls = self._event_map.get(key)
                if event_cls is None:
                    log.debug("Skipping unregistered event %s", key)
                    continue

                contract = await rp.get_contract_by_address(event["address"])
                assert contract is not None
                topics = [w3.to_hex(t) for t in event["topics"]]
                decoded = dict(
                    contract.events[solidity_event_name]().process_log(event)
                )
                decoded["topics"] = topics
                decoded["args"] = dict(decoded["args"])
                hash_args(decoded["args"])

                # Carry over aggregation attributes
                if "assignmentCount" in event:
                    decoded["assignmentCount"] = event["assignmentCount"]
                if "amountOfStETH" in event:
                    decoded["args"]["amountOfStETH"] = event["amountOfStETH"]

                event = decoded
            elif event.get("event") in self._global_event_map:
                # Global event path (already decoded by filter)
                solidity_event_name = event["event"]
                event_cls = self._global_event_map[solidity_event_name]
                event["args"] = dict(event.get("args", {}))
                hash_args(event["args"])

                # Check for upgrade events
                if event_cls.event_name in _UPGRADE_EVENTS:
                    log.info("detected contract upgrade")
                    upgrade_block = event["blockNumber"]

                # Global event enrichment (minipool/megapool validation, pubkey, sender)
                if (
                    event_cls.event_name not in _UPGRADE_EVENTS
                    and event_cls.event_name != "sdao_upgrade_vetoed_event"
                ):
                    enriched = await self._enrich_global_event(event)
                    if not enriched:
                        continue
            else:
                log.debug("Skipping event %s", event)
                continue

            # Build args dict for the event class
            args: dict[str, Any] = {
                **event.get("args", {}),
                "transactionHash": event["transactionHash"].hex()
                if isinstance(event["transactionHash"], (bytes, HexBytes))
                else event["transactionHash"],
                "blockNumber": event["blockNumber"],
                "event_name": event_cls.event_name,
            }

            # Resolve dispatchers
            resolved = await event_cls.resolve(args, event)
            if resolved is None:
                continue
            event_cls = resolved
            event_name: str = event_cls.event_name

            # Get receipt for mainnet fee calculation
            receipt: TxReceipt | None = None
            if cfg.rocketpool.chain == "mainnet":
                tx_hash = event["transactionHash"]
                if isinstance(tx_hash, str):
                    receipt = await w3.eth.get_transaction_receipt(tx_hash)
                else:
                    receipt = await w3.eth.get_transaction_receipt(tx_hash)

            try:
                embeds = await event_cls.build_embeds(args, event, receipt)
            except BadFunctionCallOutput as e:
                log.exception("Failed to build embeds for %s", event_name)
                await self.bot.report_error(e)
                continue

            # Event name may have been mutated by build_embeds
            event_name = args.get("event_name", event_name)

            if not embeds:
                continue

            # Compute tx_log_index offset
            identical_events = [
                e
                for e in aggregated
                if (
                    e.get("transactionHash") == event.get("transactionHash")
                    and e.get("blockHash") == event.get("blockHash")
                )
            ]
            tx_log_index = event.get("logIndex", 0) - min(
                e.get("logIndex", 0) for e in identical_events
            )

            tx_hash_hex = (
                event["transactionHash"].hex()
                if isinstance(event["transactionHash"], (bytes, HexBytes))
                else event["transactionHash"]
            )

            for embed in embeds:
                response = Event(
                    embed=embed,
                    topic="events",
                    event_name=event_name,
                    unique_id=f"{tx_hash_hex}:{event_name}:{args_hash.hexdigest()}:{tx_log_index}",
                    block_number=event["blockNumber"],
                    transaction_index=event.get("transactionIndex", 999),
                    event_index=event.get("logIndex", 999),
                )
                messages.append(response)

        return messages, upgrade_block

    async def _enrich_global_event(self, event: dict[str, Any]) -> bool:
        """Enrich a global event with minipool/megapool validation, pubkey, and sender.

        Returns False if the event should be skipped.
        """
        receipt = await w3.eth.get_transaction_receipt(event["transactionHash"])

        is_minipool_event = await rp.is_minipool(
            event["address"]
        ) or await rp.is_minipool(receipt["to"])
        is_megapool_event = await rp.is_megapool(
            event["address"]
        ) or await rp.is_megapool(receipt["to"])

        if not any(
            [
                is_minipool_event,
                is_megapool_event,
                rp.get_name_by_address(receipt["to"]) not in [None, "multicall3"],
                rp.get_name_by_address(event["address"]),
            ]
        ):
            log.warning(
                "Skipping %s because the called contract is not a minipool",
                event["transactionHash"].hex(),
            )
            return False

        pubkey = None

        # is the pubkey in the event arguments?
        if "validatorPubkey" in event["args"]:
            pubkey = event["args"]["validatorPubkey"].hex()

        # maybe the contract has it stored?
        if not pubkey:
            pubkey = (
                await rp.call(
                    "rocketMinipoolManager.getMinipoolPubkey", event["address"]
                )
            ).hex()

        # maybe it's in the transaction?
        if not pubkey:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                deposit_contract = await rp.get_contract_by_name("casperDeposit")
                processed_logs = deposit_contract.events.DepositEvent().process_receipt(
                    receipt
                )

            if processed_logs:
                deposit_event = processed_logs[0]
                pubkey = deposit_event.args.pubkey.hex()

        if pubkey:
            event["args"]["pubkey"] = "0x" + pubkey

        # Add sender/caller
        event["args"]["from"] = receipt["from"]
        n = rp.get_name_by_address(receipt["to"])
        if n is None or not n.startswith("rocket"):
            event["args"]["from"] = receipt["to"]
            event["args"]["caller"] = receipt["from"]

        if is_minipool_event:
            event["args"]["minipool"] = event["address"]
        if is_megapool_event:
            event["args"]["megapool"] = event["address"]
            event["args"]["node"] = await rp.call(
                "rocketMegapoolDelegate.getNodeAddress", address=event["address"]
            )

        return True

    # --- Aggregation ---

    async def aggregate_events(
        self, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Aggregate and deduplicate events within the same transaction."""
        events_by_tx: dict[Any, list[dict[str, Any]]] = {}
        for event in reversed(events):
            tx_hash = event["transactionHash"]
            if tx_hash not in events_by_tx:
                events_by_tx[tx_hash] = []
            events_by_tx[tx_hash].append(event)

        aggregation_attributes = {
            "rocketDepositPool.DepositAssigned": "assignmentCount",
            "unstETH.WithdrawalRequested": "amountOfStETH",
        }

        async def get_event_name(
            _event: dict[str, Any],
        ) -> tuple[str, str]:
            if "topics" in _event:
                contract_name = rp.get_name_by_address(_event["address"])
                name = self._topic_map[_event["topics"][0].hex()]
            else:
                contract_name = None
                name = _event.get("event", "")

            full_name = f"{contract_name}.{name}" if contract_name else name
            return name, full_name

        aggregates: dict[Any, dict[str, Any]] = {}
        for tx_hash, tx_events in events_by_tx.items():
            tx_aggregates: dict[str, Any] = {}
            aggregates[tx_hash] = tx_aggregates
            events_by_name: dict[str, list[dict[str, Any]]] = {}

            for event in tx_events:
                event_name, full_event_name = await get_event_name(event)
                log.debug("Processing event %s", full_event_name)

                if full_event_name not in events_by_name:
                    events_by_name[full_event_name] = []

                if full_event_name == "unstETH.WithdrawalRequested":
                    contract = await rp.get_contract_by_address(event["address"])
                    _event = dict(contract.events[event_name]().process_log(event))
                    amount = tx_aggregates.get(full_event_name, 0)
                    if amount:
                        events.remove(event)
                    tx_aggregates[full_event_name] = (
                        amount + _event["args"]["amountOfStETH"]
                    )
                elif full_event_name == "rocketTokenRETH.Transfer":
                    conflicting_events = [
                        "rocketTokenRETH.TokensBurned",
                        "rocketDepositPool.DepositReceived",
                    ]
                    if any(event in events_by_name for event in conflicting_events):
                        events.remove(event)
                        continue
                    if prev_event := tx_aggregates.get(full_event_name):
                        contract = await rp.get_contract_by_address(event["address"])
                        _event = dict(contract.events[event_name]().process_log(event))
                        _prev_event = dict(
                            contract.events[event_name]().process_log(prev_event)
                        )
                        if _prev_event["args"]["value"] > _event["args"]["value"]:
                            events.remove(event)
                            event = prev_event
                        else:
                            events.remove(prev_event)
                    tx_aggregates[full_event_name] = event
                elif full_event_name == "StatusUpdated":
                    if "MinipoolScrubbed" in events_by_name:
                        events.remove(event)
                        continue
                elif (
                    full_event_name
                    == "rocketDAOProtocolProposal.ProposalVoteOverridden"
                ):
                    vote_event = events_by_name.get(
                        "rocketDAOProtocolProposal.ProposalVoted", [None]
                    ).pop()
                    if vote_event is not None:
                        events.remove(vote_event)
                elif full_event_name == "MinipoolPrestaked":
                    for assign_event in events_by_name.get(
                        "rocketDepositPool.DepositAssigned", []
                    ).copy():
                        assigned_minipool = w3.to_checksum_address(
                            assign_event["topics"][1][-20:]
                        )
                        if event["address"] == assigned_minipool:
                            events_by_name["rocketDepositPool.DepositAssigned"].remove(
                                assign_event
                            )
                            events.remove(assign_event)
                            tx_aggregates["rocketDepositPool.DepositAssigned"] -= 1
                elif full_event_name in aggregation_attributes:
                    count = tx_aggregates.get(full_event_name, 0)
                    if count:
                        events.remove(event)
                    tx_aggregates[full_event_name] = count + 1
                else:
                    tx_aggregates[full_event_name] = (
                        tx_aggregates.get(full_event_name, 0) + 1
                    )

                if event in events:
                    events_by_name[full_event_name].append(event)

        result: list[dict[str, Any]] = [dict(event) for event in events]
        for event in result:
            _, full_event_name = await get_event_name(event)
            if full_event_name not in aggregation_attributes:
                continue

            tx_hash = event["transactionHash"]
            aggregated_value = aggregates[tx_hash].get(full_event_name, None)
            if aggregated_value is None:
                continue

            event[aggregation_attributes[full_event_name]] = aggregated_value

        return result


async def setup(bot: RocketWatch) -> None:
    cog = LogEvents(bot)
    await cog.async_init()
    await bot.add_cog(cog)
