from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import warnings
from collections.abc import Callable, Coroutine
from typing import Any, Literal, cast

from discord import Interaction
from discord.app_commands import Choice, command, guilds
from discord.ext.commands import is_owner
from discord.ui import Modal, TextInput
from eth_typing import HexStr
from eth_typing.evm import BlockNumber, ChecksumAddress
from hexbytes import HexBytes
from web3.constants import ADDRESS_ZERO, HASH_ZERO
from web3.contract.async_contract import AsyncContractEvent
from web3.exceptions import BadFunctionCallOutput
from web3.logs import DISCARD
from web3.types import EventData, FilterParams, LogReceipt, TxReceipt, Wei

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.config import cfg
from rocketwatch.utils.event import Event, EventPlugin
from rocketwatch.utils.rocketpool import NoAddressFound, rp
from rocketwatch.utils.shared_w3 import w3

from .aggregation import aggregate_events
from .event_definitions import (
    EVENT_REGISTRY,
    LogEvent,
    LogEventContext,
    LogEventData,
)

log = logging.getLogger("rocketwatch.log_events")

_DUMMY_RECEIPT: TxReceipt = {
    "blockHash": HexBytes(HASH_ZERO),
    "blockNumber": BlockNumber(0),
    "contractAddress": None,
    "cumulativeGasUsed": 0,
    "effectiveGasPrice": Wei(0),
    "gasUsed": 0,
    "from": ChecksumAddress(ADDRESS_ZERO),
    "logs": [],
    "logsBloom": HexBytes(b""),
    "root": HexStr(""),
    "status": 1,
    "to": ChecksumAddress(ADDRESS_ZERO),
    "transactionHash": HexBytes(HASH_ZERO),
    "transactionIndex": 0,
    "type": 0,
}

_DUMMY_EVENT: LogEventData = {
    "address": ChecksumAddress(ADDRESS_ZERO),
    "args": {},
    "blockHash": HexBytes(HASH_ZERO),
    "blockNumber": 0,
    "event": "",
    "logIndex": 0,
    "transactionHash": HexBytes(HASH_ZERO),
    "transactionIndex": 0,
}


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
        self.param_inputs: list[TextInput[_PreviewLogModal]] = []
        for name, required in fields:
            text_input: TextInput[_PreviewLogModal] = TextInput(
                label=name[:45], required=required
            )
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
        event_data: LogEventData = {**_DUMMY_EVENT, "blockNumber": self.block_number}
        resolved = await self.event_cls.resolve(args, event_data)
        if resolved is None:
            await interaction.followup.send(content="Event filtered out.")
            return
        embeds = await resolved.build_embeds(args, event_data, _DUMMY_RECEIPT)
        if embeds:
            await interaction.followup.send(embeds=embeds)
        else:
            await interaction.followup.send(content="No events triggered.")


PartialFilter = Callable[
    [BlockNumber, BlockNumber | Literal["latest"]],
    Coroutine[Any, Any, list[LogReceipt] | list[EventData]],
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
        global_topic_decoders: dict[str, AsyncContractEvent] = {}

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
            ) -> list[LogReceipt]:
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
            ) -> list[EventData]:
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
            event_data: LogEventData = {**_DUMMY_EVENT, "blockNumber": block_number}
            resolved = await event_cls.resolve(args, event_data)
            if resolved is None:
                await interaction.followup.send(content="Event filtered out.")
                return
            embeds = await resolved.build_embeds(args, event_data, _DUMMY_RECEIPT)
            if embeds:
                await interaction.followup.send(embeds=embeds)
            else:
                await interaction.followup.send(content="No events triggered.")

    @preview_log_event.autocomplete("contract")
    async def _autocomplete_contract(
        self, interaction: Interaction, current: str
    ) -> list[Choice[str]]:
        if not current and interaction.namespace.event:
            return []
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
        receipt: TxReceipt = await w3.eth.get_transaction_receipt(HexStr(tx_hash))
        logs: list[LogReceipt] = receipt["logs"]

        filtered_events: list[LogReceipt | EventData] = []

        # get direct events
        for event_log in logs:
            topics = event_log.get("topics", [])
            if topics and (topics[0].hex() in self._topic_map):
                filtered_events.append(event_log)

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
            embeds = [response.embed for response in responses]
            for i in range(0, len(embeds), 10):
                await interaction.followup.send(embeds=embeds[i : i + 10])
        else:
            await interaction.followup.send(content="No events found.")

    # --- EventPlugin lifecycle ---

    async def _get_new_events(self) -> list[Event]:
        from_block = self.last_served_block + 1 - self.lookback_distance
        return await self.get_past_events(BlockNumber(from_block), self._pending_block)

    async def get_past_events(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[Event]:
        log.debug("Fetching events in [%s, %s]", from_block, to_block)
        log.debug("Using %d filters", len(self._partial_filters))

        events: list[LogReceipt | EventData] = []
        for pf in self._partial_filters:
            events.extend(await pf(from_block, to_block))

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
        self, events: list[LogReceipt | EventData]
    ) -> tuple[list[Event], BlockNumber | None]:
        events.sort(key=lambda e: (e["blockNumber"], e["logIndex"]))
        messages: list[Event] = []
        upgrade_block: BlockNumber | None = None

        log.debug("Aggregating %d events", len(events))
        aggregated: list[dict[str, Any]] = await aggregate_events(
            events, self._topic_map
        )
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
            processed: dict[str, Any]
            if contract_name and "topics" in event:
                # Direct event path — event is a LogReceipt
                log_receipt = cast(LogReceipt, event)
                log.debug("Found event %s for %s", log_receipt, contract_name)
                solidity_event_name = self._topic_map[log_receipt["topics"][0].hex()]
                key = f"{contract_name}.{solidity_event_name}"
                event_cls = self._event_map.get(key)
                if event_cls is None:
                    log.debug("Skipping unregistered event %s", key)
                    continue

                contract = await rp.get_contract_by_address(log_receipt["address"])
                assert contract is not None
                topics = [w3.to_hex(t) for t in log_receipt["topics"]]
                processed = dict(
                    contract.events[solidity_event_name]().process_log(log_receipt)
                )
                processed["topics"] = topics
                processed["args"] = dict(processed["args"])
                hash_args(processed["args"])
                # Carry over aggregation attributes from aggregate_events
                for k, v in event.items():
                    if k not in processed:
                        processed[k] = v

            elif event.get("event") in self._global_event_map:
                # Global event path (already decoded by filter)
                event_data = cast(EventData, event)
                solidity_event_name = event_data["event"]
                event_cls = self._global_event_map[solidity_event_name]
                processed = dict(event_data)
                processed["args"] = dict(processed.get("args", {}))
                hash_args(processed["args"])

                # Check for upgrade events
                if event_cls.event_name in _UPGRADE_EVENTS:
                    log.info("detected contract upgrade")
                    upgrade_block = BlockNumber(event_data["blockNumber"])

                # Global event enrichment (minipool/megapool validation, pubkey, sender)
                if (
                    event_cls.event_name not in _UPGRADE_EVENTS
                    and event_cls.event_name != "sdao_upgrade_vetoed_event"
                ):
                    enriched = await self._enrich_global_event(processed)
                    if not enriched:
                        continue
            else:
                log.debug("Skipping event %s", event)
                continue

            # Build args dict for the event class
            args: dict[str, Any] = {
                **processed.get("args", {}),
                "transactionHash": processed["transactionHash"].hex()
                if isinstance(processed["transactionHash"], (bytes, HexBytes))
                else processed["transactionHash"],
                "blockNumber": processed["blockNumber"],
                "event_name": event_cls.event_name,
            }

            # Resolve dispatchers
            event_data = cast(LogEventData, processed)
            resolved = await event_cls.resolve(args, event_data)
            if resolved is None:
                continue
            event_cls = resolved
            event_name: str = event_cls.event_name

            # Get receipt for mainnet fee calculation
            receipt: TxReceipt = _DUMMY_RECEIPT
            if cfg.rocketpool.chain == "mainnet":
                tx_hash = processed["transactionHash"]
                if isinstance(tx_hash, str):
                    receipt = await w3.eth.get_transaction_receipt(HexStr(tx_hash))
                else:
                    receipt = await w3.eth.get_transaction_receipt(tx_hash)

            try:
                embeds = await event_cls.build_embeds(args, event_data, receipt)
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
                    e.get("transactionHash") == processed.get("transactionHash")
                    and e.get("blockHash") == processed.get("blockHash")
                )
            ]
            tx_log_index = processed.get("logIndex", 0) - min(
                e.get("logIndex", 0) for e in identical_events
            )

            tx_hash_hex = (
                processed["transactionHash"].hex()
                if isinstance(processed["transactionHash"], (bytes, HexBytes))
                else processed["transactionHash"]
            )

            for embed in embeds:
                response = Event(
                    embed=embed,
                    topic="events",
                    event_name=event_name,
                    unique_id=f"{tx_hash_hex}:{event_name}:{args_hash.hexdigest()}:{tx_log_index}",
                    block_number=processed["blockNumber"],
                    transaction_index=processed.get("transactionIndex", 999),
                    event_index=processed.get("logIndex", 999),
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


async def setup(bot: RocketWatch) -> None:
    cog = LogEvents(bot)
    await cog.async_init()
    await bot.add_cog(cog)
