from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Sequence
from typing import Any, cast

import web3.exceptions
from discord import Interaction
from discord.app_commands import Choice, command, guilds
from discord.ext.commands import is_owner
from discord.ui import Modal, TextInput
from eth_typing import BlockIdentifier, BlockNumber, ChecksumAddress, HexStr
from hexbytes import HexBytes
from web3.constants import ADDRESS_ZERO, HASH_ZERO
from web3.types import BlockData, Nonce, TxData, TxReceipt, Wei

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.config import cfg
from rocketwatch.utils.dao import DefaultDAO, ProtocolDAO
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.event import Event, EventPlugin
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.shared_w3 import w3

from .event_definitions import (
    DAO_PROPOSAL_EVENTS,
    TRANSACTION_REGISTRY,
    DAOProposalExecuteEvent,
    EventContext,
    ProposalExecuteEvent,
    TransactionEvent,
    TxEventData,
    UpgradeTriggeredEvent,
)

log = logging.getLogger("rocketwatch.tx_events")

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

_DUMMY_EVENT: TxEventData = {
    "blockHash": HexBytes(HASH_ZERO),
    "blockNumber": BlockNumber(0),
    "from": ChecksumAddress(ADDRESS_ZERO),
    "gas": 0,
    "gasPrice": Wei(0),
    "hash": HexBytes(HASH_ZERO),
    "input": HexBytes(b""),
    "nonce": Nonce(0),
    "to": ChecksumAddress(ADDRESS_ZERO),
    "transactionIndex": 0,
    "value": Wei(0),
}


def _get_event_fields(
    event_cls: TransactionEvent,
) -> list[tuple[str, bool]]:
    """Return ``[(name, required), ...]`` for non-context fields of *event_cls*'s Args."""
    args_type = type(event_cls).Args
    if args_type is EventContext:
        return []
    context_keys = set(EventContext.__annotations__)
    return [
        (name, name in args_type.__required_keys__)
        for name in args_type.__annotations__
        if name not in context_keys
    ]


class PreviewTxModal(Modal):
    def __init__(
        self,
        event_cls: TransactionEvent,
        function: str,
        block_number: BlockNumber,
        fields: list[tuple[str, bool]],
    ) -> None:
        super().__init__(title=event_cls.event_name[:45])
        self.event_cls = event_cls
        self.function = function
        self.block_number = block_number
        self.fields = fields
        self.param_inputs: list[TextInput[PreviewTxModal]] = []
        for name, required in fields:
            text_input: TextInput[PreviewTxModal] = TextInput(
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

        event_data: TxEventData = {**_DUMMY_EVENT, "blockNumber": self.block_number}
        args: dict[str, Any] = {
            **parsed_args,
            "function_name": self.function,
            "event_name": self.event_cls.event_name,
            "transactionHash": HASH_ZERO,
            "blockNumber": self.block_number,
        }
        embeds = await self.event_cls.build_embeds(args, event_data, _DUMMY_RECEIPT)
        if embeds:
            await interaction.followup.send(embeds=embeds)
        else:
            await interaction.followup.send(content="No events triggered.")


class TxEvents(EventPlugin):
    def __init__(self, bot: RocketWatch) -> None:
        super().__init__(bot)
        self.addresses: list[ChecksumAddress] | None = None

    async def _ensure_config(self) -> None:
        if self.addresses is None:
            self.addresses = await self._parse_transaction_config()

    @staticmethod
    async def _parse_transaction_config() -> list[ChecksumAddress]:
        addresses: list[ChecksumAddress] = []
        for contract_name in TRANSACTION_REGISTRY:
            try:
                addresses.append(await rp.get_address_by_name(contract_name))
            except Exception:
                log.warning("Could not find address for contract %s", contract_name)
        return addresses

    # --- Slash commands ---

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def preview_tx_event(
        self,
        interaction: Interaction,
        contract: str,
        function: str,
        block_number: int = 0,
    ) -> None:
        event_cls = TRANSACTION_REGISTRY.get(contract, {}).get(function)
        if event_cls is None:
            await interaction.response.send_message(
                content="No event registered for that contract/function."
            )
            return

        block_number = BlockNumber(block_number)
        fields = _get_event_fields(event_cls)
        if fields:
            modal = PreviewTxModal(event_cls, function, block_number, fields)
            await interaction.response.send_modal(modal)
        else:
            await interaction.response.defer()
            event_data: TxEventData = {**_DUMMY_EVENT, "blockNumber": block_number}
            args: EventContext = {
                "function_name": function,
                "event_name": event_cls.event_name,
                "transactionHash": HASH_ZERO,
                "blockNumber": block_number,
                "timestamp": 0,
            }
            embeds = await event_cls.build_embeds(args, event_data, _DUMMY_RECEIPT)
            if embeds:
                await interaction.followup.send(embeds=embeds)
            else:
                await interaction.followup.send(content="No events triggered.")

    @preview_tx_event.autocomplete("contract")
    async def _autocomplete_contract(
        self, interaction: Interaction, current: str
    ) -> list[Choice[str]]:
        if not current and interaction.namespace.function:
            return []
        return [
            Choice(name=name, value=name)
            for name in TRANSACTION_REGISTRY
            if current.lower() in name.lower()
        ][:25]

    @preview_tx_event.autocomplete("function")
    async def _autocomplete_function(
        self, interaction: Interaction, current: str
    ) -> list[Choice[str]]:
        contract = interaction.namespace.contract or ""
        functions = TRANSACTION_REGISTRY.get(contract, {})
        return [
            Choice(name=name, value=name)
            for name in functions
            if current.lower() in name.lower()
        ][:25]

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def replay_tx_events(self, interaction: Interaction, tx_hash: str) -> None:
        await interaction.response.defer()
        if not tx_hash.startswith("0x") or len(tx_hash) != 66:
            await interaction.followup.send(content="Invalid transaction hash.")
            return
        await self._ensure_config()
        txn: TxData = await w3.eth.get_transaction(HexStr(tx_hash))
        block: BlockData = await w3.eth.get_block(txn["blockHash"])

        responses: list[Event] = await self.process_transaction(
            block, txn, txn["to"], txn["input"]
        )
        if responses:
            await interaction.followup.send(
                embeds=[response.embed for response in responses]
            )
        else:
            await interaction.followup.send(content="No events found.")

    # --- EventPlugin lifecycle ---

    async def _get_new_events(self) -> list[Event]:
        await self._ensure_config()
        old_addresses = self.addresses
        try:
            from_block = BlockNumber(
                self.last_served_block + 1 - self.lookback_distance
            )
            return await self.get_past_events(from_block, self._pending_block)
        except Exception as err:
            # rollback in case of contract upgrade
            self.addresses = old_addresses
            raise err

    async def get_past_events(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[Event]:
        await self._ensure_config()
        events: list[Event] = []
        for block in range(from_block, to_block):
            events.extend(await self.get_events_for_block(block))
        return events

    async def get_events_for_block(self, block_number: BlockIdentifier) -> list[Event]:
        log.debug("Checking block %s", block_number)
        try:
            block: BlockData = await w3.eth.get_block(
                block_number, full_transactions=True
            )
        except web3.exceptions.BlockNotFound:
            log.error("Skipping block %s as it can't be found", block_number)
            return []

        # full_transactions=True guarantees Sequence[TxData], not Sequence[HexBytes]
        transactions = cast(Sequence[TxData], block.get("transactions", []))
        events: list[Event] = []
        for txn in transactions:
            if "to" in txn:
                events.extend(
                    await self.process_transaction(block, txn, txn["to"], txn["input"])
                )
            else:
                log.debug(
                    "Skipping transaction %s as it has no `to` parameter. "
                    "Possible contract creation.",
                    txn["hash"].hex(),
                )

        return events

    # --- Transaction processing ---

    async def process_transaction(
        self,
        block: BlockData,
        txn: TxData,
        contract_address: ChecksumAddress,
        fn_input: HexBytes,
    ) -> list[Event]:
        assert self.addresses is not None
        if contract_address not in self.addresses:
            return []

        contract_name = rp.get_name_by_address(contract_address)
        if contract_name is None:
            return []
        receipt: TxReceipt = await w3.eth.get_transaction_receipt(txn["hash"])

        if not self._should_process(contract_name, receipt, txn):
            return []

        decoded = await self._decode_function(contract_address, fn_input, txn)
        if decoded is None:
            return []
        event_cls, function_name, decoded_args = decoded

        event: TxEventData = self._build_event(txn, block, decoded_args, function_name)

        payload_events: list[Event] = []
        if isinstance(event_cls, ProposalExecuteEvent):
            payload_events = await self._handle_dao_proposal(
                event_cls, event, block, txn
            )

        args: dict[str, Any] = {
            **event["args"],
            "event_name": event_cls.event_name,
            "transactionHash": event["hash"].to_0x_hex(),
            "blockNumber": event["blockNumber"],
        }

        embeds = await event_cls.build_embeds(args, event, receipt)

        responses = self._wrap_embeds(
            embeds, event_cls.event_name, txn, event, payload_events
        )

        if isinstance(event_cls, UpgradeTriggeredEvent):
            await self._handle_upgrade(event["blockNumber"])

        return responses

    @staticmethod
    def _should_process(contract_name: str, receipt: TxReceipt, txn: TxData) -> bool:
        if contract_name == "rocketNodeDeposit" and receipt["status"]:
            log.info("Skipping successful node deposit %s", txn["hash"].hex())
            return False
        if contract_name != "rocketNodeDeposit" and not receipt["status"]:
            log.info("Skipping reverted transaction %s", txn["hash"].hex())
            return False
        return True

    async def _decode_function(
        self,
        contract_address: ChecksumAddress,
        fn_input: HexBytes,
        txn: TxData,
    ) -> tuple[TransactionEvent, str, dict[str, Any]] | None:
        try:
            contract = await rp.get_contract_by_address(contract_address)
            assert contract is not None
            decoded = contract.decode_function_input(fn_input)
        except ValueError:
            log.error(
                "Skipping transaction %s as it has invalid input", txn["hash"].hex()
            )
            return None
        log.debug(decoded)

        function: str = decoded[0].abi_element_identifier
        function_name: str = function.split("(")[0]
        contract_name = rp.get_name_by_address(contract_address)
        if contract_name is None:
            return None

        event_cls = TRANSACTION_REGISTRY.get(contract_name, {}).get(function_name)
        if event_cls is None:
            return None

        decoded_args: dict[str, Any] = {
            arg.lstrip("_"): value for arg, value in decoded[1].items()
        }

        # Resolve DAO proposal prefix: swap DAOProposalExecuteEvent for the
        # appropriate odao/sdao ProposalExecuteEvent
        if isinstance(event_cls, DAOProposalExecuteEvent):
            dao_name: str = await rp.call(
                "rocketDAOProposal.getDAO", decoded_args["proposalID"]
            )
            event_cls = DAO_PROPOSAL_EVENTS[dao_name]

        return event_cls, function_name, decoded_args

    @staticmethod
    def _build_event(
        txn: TxData,
        block: BlockData,
        decoded_args: dict[str, Any],
        function_name: str,
    ) -> TxEventData:
        event = cast(TxEventData, {**txn})
        event["args"] = decoded_args
        event["args"]["timestamp"] = block["timestamp"]
        event["args"]["function_name"] = function_name
        return event

    async def _handle_dao_proposal(
        self,
        event_cls: ProposalExecuteEvent,
        event: TxEventData,
        block: BlockData,
        txn: TxData,
    ) -> list[Event]:
        proposal_id: int = event["args"]["proposalID"]
        dao: ProtocolDAO | DefaultDAO
        if "pdao" in event_cls.event_name:
            dao = ProtocolDAO()
            payload: HexBytes = await rp.call(
                "rocketDAOProtocolProposal.getPayload", proposal_id
            )
        else:
            dao = DefaultDAO(await rp.call("rocketDAOProposal.getDAO", proposal_id))
            payload = await rp.call("rocketDAOProposal.getPayload", proposal_id)

        event["args"]["executor"] = event["from"]
        proposal = await dao.fetch_proposal(proposal_id)
        event["args"]["proposal_body"] = await dao.build_proposal_body(
            proposal, include_proposer=False
        )

        dao_contract = await dao._get_contract()
        dao_address: ChecksumAddress = dao_contract.address
        return await self.process_transaction(block, txn, dao_address, payload)

    @staticmethod
    def _wrap_embeds(
        embeds: list[Embed],
        event_name: str,
        txn: TxData,
        event: TxEventData,
        child_responses: list[Event],
    ) -> list[Event]:
        responses: list[Event] = []
        for embed in embeds:
            response = Event(
                topic="transactions",
                embed=embed,
                event_name=event_name,
                unique_id=f"{txn['hash'].hex()}:{event_name}",
                block_number=event["blockNumber"],
                transaction_index=event["transactionIndex"],
                event_index=(999 - len(child_responses) - len(embeds) + len(responses)),
            )
            responses.append(response)
        return responses + child_responses

    async def _handle_upgrade(self, block_number: int) -> None:
        log.info("Detected contract upgrade at block %s, reinitializing", block_number)
        await rp.flush()
        self.addresses = await self._parse_transaction_config()


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(TxEvents(bot))
