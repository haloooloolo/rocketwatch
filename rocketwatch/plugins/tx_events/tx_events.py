from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, cast

import web3.exceptions
from discord import Interaction
from discord.app_commands import Choice, command, guilds
from discord.ext.commands import is_owner
from eth_typing import BlockIdentifier, BlockNumber, ChecksumAddress, HexStr
from hexbytes import HexBytes
from web3.constants import ADDRESS_ZERO, HASH_ZERO
from web3.types import BlockData, Nonce, TxData, TxReceipt, Wei

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.chain_event import (
    PreviewModal,
    posted_name,
    preview_fields,
    send_preview,
)
from rocketwatch.utils.config import cfg
from rocketwatch.utils.dao import DefaultDAO, ProtocolDAO
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.event import Event, EventPlugin
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.shared_w3 import w3

from .event_definitions import (
    TRANSACTION_REGISTRY,
    EventContext,
    TransactionEvent,
    TxEventData,
)

log = logging.getLogger("rocketwatch.tx_events")

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


def _context(
    handler: TransactionEvent,
    function_name: str,
    tx_hash: HexStr,
    block_number: BlockNumber,
    timestamp: int,
) -> EventContext:
    return {
        "transactionHash": tx_hash,
        "blockNumber": block_number,
        "event_name": handler.event_name,
        "function_name": function_name,
        "timestamp": timestamp,
    }


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

        block = BlockNumber(block_number)
        context = _context(event_cls, function, HexStr(HASH_ZERO), block, 0)
        event: TxEventData = {**_DUMMY_EVENT, "blockNumber": block}

        async def render(interaction: Interaction, args: dict[str, Any]) -> None:
            await send_preview(interaction, event_cls, {**args, **context}, event)

        if fields := preview_fields(event_cls, EventContext):
            await interaction.response.send_modal(
                PreviewModal(event_cls, fields, render)
            )
        else:
            await interaction.response.defer()
            await render(interaction, {})

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

        decoded = await self._decode_function(
            contract_name, contract_address, fn_input, txn
        )
        if decoded is None:
            return []
        handler, function_name, decoded_args = decoded

        receipt: TxReceipt = await w3.eth.get_transaction_receipt(txn["hash"])
        if bool(receipt["status"]) == handler.reverted_only:
            log.info(
                "Skipping %s transaction %s",
                "successful" if receipt["status"] else "reverted",
                txn["hash"].hex(),
            )
            return []

        event = cast(TxEventData, {**txn, "args": decoded_args})
        args: dict[str, Any] = {
            **decoded_args,
            **_context(
                handler,
                function_name,
                HexStr(txn["hash"].to_0x_hex()),
                BlockNumber(txn["blockNumber"]),
                block["timestamp"],
            ),
        }
        resolved = await handler.resolve(args, event)
        if resolved is None:
            return []
        assert isinstance(resolved, TransactionEvent)
        args["event_name"] = resolved.event_name

        payload_events: list[Event] = []
        if resolved.executes_payload:
            payload_events = await self._process_proposal_payload(
                resolved, args, block, txn
            )

        embeds = await resolved.build_embeds(args, event, receipt)
        responses = self._wrap_embeds(
            embeds, posted_name(resolved, embeds), txn, payload_events
        )

        if resolved.reloads_contracts:
            await self._handle_upgrade(txn["blockNumber"])

        return responses

    async def _decode_function(
        self,
        contract_name: str,
        contract_address: ChecksumAddress,
        fn_input: HexBytes,
        txn: TxData,
    ) -> tuple[TransactionEvent, str, dict[str, Any]] | None:
        try:
            contract = await rp.get_contract_by_address(contract_address)
            assert contract is not None
            function, raw_args = contract.decode_function_input(fn_input)
        except ValueError:
            log.error(
                "Skipping transaction %s as it has invalid input", txn["hash"].hex()
            )
            return None

        function_name: str = function.abi_element_identifier.split("(")[0]
        handler = TRANSACTION_REGISTRY.get(contract_name, {}).get(function_name)
        if handler is None:
            return None

        decoded_args = {arg.lstrip("_"): value for arg, value in raw_args.items()}
        return handler, function_name, decoded_args

    async def _process_proposal_payload(
        self,
        handler: TransactionEvent,
        args: dict[str, Any],
        block: BlockData,
        txn: TxData,
    ) -> list[Event]:
        """Add the proposal's details to *args* and process the call it executed."""
        proposal_id: int = args["proposalID"]
        dao: ProtocolDAO | DefaultDAO
        if "pdao" in handler.event_name:
            dao = ProtocolDAO()
            payload: HexBytes = await rp.call(
                "rocketDAOProtocolProposal.getPayload", proposal_id
            )
        else:
            dao = DefaultDAO(await rp.call("rocketDAOProposal.getDAO", proposal_id))
            payload = await rp.call("rocketDAOProposal.getPayload", proposal_id)

        args["executor"] = txn["from"]
        proposal = await dao.fetch_proposal(proposal_id)
        args["proposal_body"] = await dao.build_proposal_body(
            proposal, include_proposer=False
        )

        dao_contract = await dao._get_contract()
        return await self.process_transaction(block, txn, dao_contract.address, payload)

    @staticmethod
    def _wrap_embeds(
        embeds: list[Embed],
        event_name: str,
        txn: TxData,
        payload_events: list[Event],
    ) -> list[Event]:
        events = [
            Event(
                topic="transactions",
                embed=embed,
                event_name=event_name,
                unique_id=f"{txn['hash'].hex()}:{event_name}:{i}",
                block_number=txn["blockNumber"],
                transaction_index=txn["transactionIndex"],
            )
            for i, embed in enumerate(embeds)
        ] + payload_events
        # after the transaction's log events, which are indexed by log index;
        # a proposal comes before the payload it executed
        return [
            replace(e, event_index=999 - len(events) + i) for i, e in enumerate(events)
        ]

    async def _handle_upgrade(self, block_number: int) -> None:
        log.info("Detected contract upgrade at block %s, reinitializing", block_number)
        await rp.flush()
        self.addresses = await self._parse_transaction_config()


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(TxEvents(bot))
