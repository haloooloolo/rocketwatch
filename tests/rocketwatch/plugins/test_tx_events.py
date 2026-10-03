from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_typing import BlockNumber
from hexbytes import HexBytes

from rocketwatch.plugins.tx_events import event_definitions as defs
from rocketwatch.plugins.tx_events import tx_events as txm
from rocketwatch.plugins.tx_events.event_definitions import TRANSACTION_REGISTRY
from rocketwatch.plugins.tx_events.tx_events import (
    PreviewTxModal,
    TxEvents,
    _get_event_fields,
)
from rocketwatch.utils import shared_w3
from rocketwatch.utils.embeds import Embed
from tests.lib.discord_harness import make_bot, make_interaction
from tests.lib.explorer import stub_explorer_links
from tests.lib.scripted_rocketpool import ScriptedRocketPool, addr


def _txn(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "hash": HexBytes(b"\xab" * 32),
        "blockNumber": 100,
        "transactionIndex": 3,
        "from": addr("0x" + "11" * 20),
        "input": HexBytes(b""),
    }
    base.update(overrides)
    return base


class TestShouldProcess:
    def test_skips_successful_node_deposit(self) -> None:
        receipt = {"status": 1}
        assert TxEvents._should_process("rocketNodeDeposit", receipt, _txn()) is False

    def test_keeps_reverted_node_deposit(self) -> None:
        receipt = {"status": 0}
        assert TxEvents._should_process("rocketNodeDeposit", receipt, _txn()) is True

    def test_skips_reverted_non_deposit(self) -> None:
        receipt = {"status": 0}
        assert TxEvents._should_process("rocketDAOProposal", receipt, _txn()) is False

    def test_keeps_successful_non_deposit(self) -> None:
        receipt = {"status": 1}
        assert TxEvents._should_process("rocketDAOProposal", receipt, _txn()) is True


class TestBuildEvent:
    def test_merges_txn_args_and_block_metadata(self) -> None:
        txn = _txn()
        block = {"timestamp": 1_700_000_000}
        event = TxEvents._build_event(txn, block, {"amount": 5}, "deposit")
        assert event["args"]["amount"] == 5
        assert event["args"]["timestamp"] == 1_700_000_000
        assert event["args"]["function_name"] == "deposit"
        # Original txn keys are preserved.
        assert event["transactionIndex"] == 3


class TestWrapEmbeds:
    def test_wraps_each_embed_into_event(self) -> None:
        txn = _txn()
        event = {"blockNumber": 100, "transactionIndex": 3}
        embeds = [Embed(title="a"), Embed(title="b")]
        responses = TxEvents._wrap_embeds(embeds, "my_event", txn, event, [])
        assert len(responses) == 2
        assert all(r.event_name == "my_event" for r in responses)
        assert all(r.block_number == 100 for r in responses)
        assert responses[0].embed.title == "a"

    def test_each_embed_is_stored_separately(self) -> None:
        # event_core keys stored events by unique_id; a shared id drops all
        # but the first embed (e.g. a claim from several treasury contracts)
        event = {"blockNumber": 100, "transactionIndex": 3}
        embeds = [Embed(title="a"), Embed(title="b"), Embed(title="c")]

        responses = TxEvents._wrap_embeds(embeds, "my_event", _txn(), event, [])

        assert len({r.unique_id for r in responses}) == 3

    def test_appends_child_responses(self) -> None:
        txn = _txn()
        event = {"blockNumber": 100, "transactionIndex": 3}
        child = TxEvents._wrap_embeds([Embed(title="child")], "child", txn, event, [])
        responses = TxEvents._wrap_embeds(
            [Embed(title="parent")], "parent", txn, event, child
        )
        # parent embed + appended child response.
        names = [r.event_name for r in responses]
        assert names == ["parent", "child"]


class TestGetEventFields:
    def test_event_with_fields(self) -> None:
        ev = TRANSACTION_REGISTRY["rocketDAONodeTrusted"]["bootstrapMember"]
        fields = _get_event_fields(ev)
        names = [n for n, _ in fields]
        assert "nodeAddress" in names

    def test_event_without_fields(self) -> None:
        ev = TRANSACTION_REGISTRY["rocketDAOProposal"]["execute"]
        assert _get_event_fields(ev) == []


class TestParseTransactionConfig:
    async def test_resolves_known_skips_unknown(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        # Only script an address for the first registry contract; the rest
        # raise KeyError in get_address_by_name and are skipped with a warning.
        first = next(iter(TRANSACTION_REGISTRY))
        scripted_rp.set_address(first, addr("0x" + "ab" * 20))
        addresses = await TxEvents._parse_transaction_config()
        assert addr("0x" + "ab" * 20) in addresses
        # Far fewer than the full registry since others are unresolved.
        assert len(addresses) == 1


class TestPreviewCommand:
    async def test_unknown_event_reports(self) -> None:
        cog = TxEvents(make_bot())
        interaction = make_interaction()
        await cog.preview_tx_event.callback(
            cog, interaction, contract="nope", function="nope"
        )
        msg = (
            interaction.response.send_message.call_args.kwargs.get("content")
            or interaction.response.send_message.call_args.args[0]
        )
        assert "No event registered" in msg

    async def test_event_with_fields_opens_modal(self) -> None:
        cog = TxEvents(make_bot())
        interaction = make_interaction()
        interaction.response.send_modal = AsyncMock()
        await cog.preview_tx_event.callback(
            cog,
            interaction,
            contract="rocketDAONodeTrusted",
            function="bootstrapMember",
        )
        interaction.response.send_modal.assert_awaited_once()


class TestAutocomplete:
    async def test_contract_filter(self) -> None:
        cog = TxEvents(make_bot())
        interaction = make_interaction()
        interaction.namespace.function = ""
        out = await cog._autocomplete_contract(interaction, "dao")
        assert all("dao" in c.value.lower() for c in out)
        assert len(out) > 0

    async def test_contract_empty_with_function_returns_nothing(self) -> None:
        cog = TxEvents(make_bot())
        interaction = make_interaction()
        interaction.namespace.function = "execute"
        out = await cog._autocomplete_contract(interaction, "")
        assert out == []

    async def test_function_filter_for_contract(self) -> None:
        cog = TxEvents(make_bot())
        interaction = make_interaction()
        interaction.namespace.contract = "rocketDAONodeTrusted"
        out = await cog._autocomplete_function(interaction, "bootstrap")
        assert all("bootstrap" in c.value.lower() for c in out)
        assert len(out) > 0


class TestReplayTxEvents:
    async def test_rejects_invalid_hash(self) -> None:
        cog = TxEvents(make_bot())
        interaction = make_interaction()
        await cog.replay_tx_events.callback(cog, interaction, tx_hash="0xshort")
        msg = (
            interaction.followup.send.call_args.kwargs.get("content")
            or interaction.followup.send.call_args.args[0]
        )
        assert "Invalid transaction hash" in msg


class TestProcessTransaction:
    async def test_ignores_address_outside_registry(self) -> None:
        cog = TxEvents(make_bot())
        cog.addresses = [addr("0x" + "11" * 20)]
        result = await cog.process_transaction(
            {}, _txn(), addr("0x" + "99" * 20), HexBytes(b"")
        )
        assert result == []


class TestGetEventsForBlock:
    async def test_block_not_found_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import web3.exceptions

        from rocketwatch.plugins.tx_events import tx_events as txm

        async def raise_not_found(*_a: Any, **_k: Any) -> Any:
            raise web3.exceptions.BlockNotFound("missing")

        monkeypatch.setattr(
            txm.w3, "eth", AsyncMock(get_block=raise_not_found), raising=False
        )
        cog = TxEvents(make_bot())
        assert await cog.get_events_for_block(123) == []

    async def test_skips_transactions_without_to(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from rocketwatch.plugins.tx_events import tx_events as txm

        # A transaction with no "to" (contract creation) is skipped.
        block = {"transactions": [{"hash": HexBytes(b"\x01" * 32)}]}

        async def get_block(*_a: Any, **_k: Any) -> Any:
            return block

        monkeypatch.setattr(
            txm.w3, "eth", AsyncMock(get_block=get_block), raising=False
        )
        cog = TxEvents(make_bot())
        cog.addresses = []
        assert await cog.get_events_for_block(123) == []


PDAO = addr("0x" + "a1" * 20)
ODAO = addr("0x" + "a2" * 20)
PROPOSAL = addr("0x" + "a3" * 20)
PDAO_PROPOSAL = addr("0x" + "a4" * 20)
UPGRADE = addr("0x" + "a5" * 20)
SENDER = addr("0x" + "11" * 20)


class Chain:
    """Scripted blocks, receipts and calldata decoding for registry contracts."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
    ) -> None:
        self.rp = scripted_rp
        self.blocks: dict[int, dict[str, Any]] = {}
        self.statuses: dict[bytes, int] = {}
        # calldata -> (function signature, raw ABI args) or an exception
        self.calldata: dict[bytes, tuple[str, dict[str, Any]] | Exception] = {}
        for name, address in [
            ("rocketDAOProtocolProposals", PDAO),
            ("rocketDAONodeTrustedProposals", ODAO),
            ("rocketDAOProposal", PROPOSAL),
            ("rocketDAOProtocolProposal", PDAO_PROPOSAL),
            ("rocketUpgradeOneDotFour", UPGRADE),
        ]:
            scripted_rp.set_address(name, address)

        eth = MagicMock()
        eth.get_transaction_receipt = AsyncMock(side_effect=self._receipt)
        eth.get_block = AsyncMock(side_effect=lambda n, **_: self.blocks[n])
        eth.get_transaction = AsyncMock(side_effect=self._transaction)
        monkeypatch.setattr(shared_w3.w3, "_instance", MagicMock(eth=eth))
        monkeypatch.setattr(
            scripted_rp,
            "get_contract_by_address",
            AsyncMock(return_value=SimpleNamespace(decode_function_input=self._decode)),
            raising=False,
        )

    def _receipt(self, tx_hash: HexBytes) -> dict[str, Any]:
        return {"status": self.statuses.get(bytes(tx_hash), 1), "from": SENDER}

    def _transaction(self, tx_hash: str) -> dict[str, Any]:
        for block in self.blocks.values():
            for txn in block["transactions"]:
                if txn["hash"].to_0x_hex() == tx_hash:
                    return dict(txn)
        raise KeyError(tx_hash)

    def _decode(self, fn_input: HexBytes) -> tuple[Any, dict[str, Any]]:
        decoded = self.calldata[bytes(fn_input)]
        if isinstance(decoded, Exception):
            raise decoded
        signature, args = decoded
        return SimpleNamespace(abi_element_identifier=signature), dict(args)

    def tx(
        self,
        to: str,
        signature: str,
        args: dict[str, Any] | Exception,
        *,
        block: int = 100,
        status: int = 1,
    ) -> dict[str, Any]:
        n = len(self.calldata) + 1
        tx_hash = HexBytes(bytes([n]) * 32)
        calldata = bytes([0xC0, n])
        self.calldata[calldata] = (
            args
            if isinstance(args, Exception)
            else (
                signature,
                args,
            )
        )
        self.statuses[bytes(tx_hash)] = status
        txn = {
            "hash": tx_hash,
            "from": SENDER,
            "to": to,
            "input": HexBytes(calldata),
            "blockNumber": block,
            "blockHash": HexBytes(bytes([block % 256]) * 32),
            "transactionIndex": n,
            "gasPrice": 1,
        }
        self.blocks.setdefault(
            block, {"number": block, "timestamp": 1_700_000_000, "transactions": []}
        )["transactions"].append(txn)
        return txn

    def payload(self, signature: str, args: dict[str, Any]) -> bytes:
        calldata = bytes([0xDA, len(self.calldata)])
        self.calldata[calldata] = (signature, args)
        return calldata


SETTING = "proposalSettingUint(string,string,uint256)"
SETTING_ARGS = {
    "_settingContractName": "rocketDAOProtocolSettingsDeposit",
    "_settingPath": "deposit.fee",
    "_value": 5,
}


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool) -> Chain:
    stub_explorer_links(monkeypatch, defs)
    return Chain(monkeypatch, scripted_rp)


@pytest.fixture
async def cog(chain: Chain) -> TxEvents:
    cog = TxEvents(make_bot())
    await cog._ensure_config()
    return cog


async def _process(cog: TxEvents, chain: Chain, txn: dict[str, Any]) -> list[Any]:
    block = chain.blocks[txn["blockNumber"]]
    return await cog.process_transaction(block, txn, txn["to"], txn["input"])  # type: ignore[arg-type]


class TestProcessRegisteredTransaction:
    async def test_setting_change_becomes_one_event(
        self, cog: TxEvents, chain: Chain
    ) -> None:
        txn = chain.tx(PDAO, SETTING, SETTING_ARGS)

        [event] = await _process(cog, chain, txn)

        assert event.topic == "transactions"
        assert event.event_name == "pdao_setting"
        assert txn["hash"].hex() in event.unique_id
        assert (event.block_number, event.transaction_index) == (
            100,
            txn["transactionIndex"],
        )
        # ABI argument names lose their leading underscore
        assert event.embed.description == "Setting `deposit.fee` set to `5`!"

    async def test_reverted_transaction_is_ignored(
        self, cog: TxEvents, chain: Chain
    ) -> None:
        txn = chain.tx(PDAO, SETTING, SETTING_ARGS, status=0)

        assert await _process(cog, chain, txn) == []

    async def test_undecodable_input_is_ignored(
        self, cog: TxEvents, chain: Chain
    ) -> None:
        txn = chain.tx(PDAO, "", ValueError("bad calldata"))

        assert await _process(cog, chain, txn) == []

    async def test_unregistered_function_is_ignored(
        self, cog: TxEvents, chain: Chain
    ) -> None:
        txn = chain.tx(PDAO, "vote(uint256,uint8)", {"_proposalID": 1, "_vote": 1})

        assert await _process(cog, chain, txn) == []


class TestProposalExecution:
    @pytest.fixture
    def daos(self, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        created: list[Any] = []

        class StubDAO:
            def __init__(
                self, contract_name: str = "rocketDAOProtocolProposal"
            ) -> None:
                self.address = {
                    "rocketDAONodeTrustedProposals": ODAO,
                    "rocketDAOProtocolProposal": PDAO,
                }[contract_name]
                created.append(self)

            async def fetch_proposal(self, proposal_id: int) -> int:
                return proposal_id

            async def build_proposal_body(
                self, proposal: int, include_proposer: bool
            ) -> str:
                return f"Proposal {proposal} body"

            async def _get_contract(self) -> Any:
                return SimpleNamespace(address=self.address)

        monkeypatch.setattr(txm, "DefaultDAO", StubDAO)
        monkeypatch.setattr(txm, "ProtocolDAO", StubDAO)
        return created

    async def test_odao_execution_posts_proposal_then_its_payload(
        self,
        cog: TxEvents,
        chain: Chain,
        scripted_rp: ScriptedRocketPool,
        daos: list[Any],
    ) -> None:
        scripted_rp.set_call(
            "rocketDAOProposal.getDAO", "rocketDAONodeTrustedProposals"
        )
        scripted_rp.set_call(
            "rocketDAOProposal.getPayload", chain.payload(SETTING, SETTING_ARGS)
        )
        txn = chain.tx(PROPOSAL, "execute(uint256)", {"_proposalID": 7})

        events = await _process(cog, chain, txn)

        assert [e.event_name for e in events] == [
            "odao_proposal_execute",
            "odao_setting",
        ]
        executed, payload = events
        assert "**proposal #7**" in executed.embed.description
        assert SENDER in executed.embed.description
        assert "Proposal 7 body" in executed.embed.description
        assert payload.embed.description == "Setting `deposit.fee` set to `5`!"
        # the proposal is announced before what it did
        assert executed.get_score() < payload.get_score()

    async def test_pdao_execution_uses_protocol_payload(
        self,
        cog: TxEvents,
        chain: Chain,
        scripted_rp: ScriptedRocketPool,
        daos: list[Any],
    ) -> None:
        scripted_rp.set_call(
            "rocketDAOProtocolProposal.getPayload",
            chain.payload(SETTING, SETTING_ARGS),
        )
        txn = chain.tx(PDAO_PROPOSAL, "execute(uint256)", {"_proposalID": 3})

        events = await _process(cog, chain, txn)

        assert [e.event_name for e in events] == [
            "pdao_proposal_execute",
            "pdao_setting",
        ]


class TestUpgrade:
    async def test_upgrade_reloads_contract_addresses(
        self, cog: TxEvents, chain: Chain, scripted_rp: ScriptedRocketPool
    ) -> None:
        txn = chain.tx(UPGRADE, "execute()", {})
        upgraded = addr("0x" + "b1" * 20)
        # cached addresses would otherwise point at pre-upgrade contracts
        monkey_flush = AsyncMock(
            side_effect=lambda: scripted_rp.set_address(
                "rocketDAOProtocolProposals", upgraded
            )
        )
        scripted_rp.flush = monkey_flush  # type: ignore[method-assign]

        [event] = await _process(cog, chain, txn)

        assert event.event_name == "saturn_one_upgrade_triggered"
        monkey_flush.assert_awaited_once()
        assert cog.addresses is not None
        assert upgraded in cog.addresses


class TestBlockScan:
    async def test_collects_events_from_each_block_in_range(
        self, cog: TxEvents, chain: Chain
    ) -> None:
        chain.tx(PDAO, SETTING, SETTING_ARGS, block=10)
        chain.tx(PDAO, SETTING, SETTING_ARGS, block=11)
        chain.tx(addr("0x" + "ee" * 20), SETTING, SETTING_ARGS, block=11)
        chain.tx(PDAO, SETTING, SETTING_ARGS, block=12)

        events = await cog.get_past_events(10, 12)  # type: ignore[arg-type]

        assert [e.block_number for e in events] == [10, 11]


class TestReplayCommand:
    async def test_posts_events_of_the_transaction(
        self, cog: TxEvents, chain: Chain
    ) -> None:
        txn = chain.tx(PDAO, SETTING, SETTING_ARGS)
        chain.blocks[txn["blockHash"]] = chain.blocks[100]
        interaction = make_interaction()

        await cog.replay_tx_events.callback(
            cog, interaction, tx_hash=txn["hash"].to_0x_hex()
        )

        embeds = interaction.followup.send.call_args.kwargs["embeds"]
        assert [e.description for e in embeds] == ["Setting `deposit.fee` set to `5`!"]

    async def test_reports_when_nothing_matched(
        self, cog: TxEvents, chain: Chain
    ) -> None:
        txn = chain.tx(PDAO, "vote(uint256,uint8)", {"_proposalID": 1, "_vote": 1})
        chain.blocks[txn["blockHash"]] = chain.blocks[100]
        interaction = make_interaction()

        await cog.replay_tx_events.callback(
            cog, interaction, tx_hash=txn["hash"].to_0x_hex()
        )

        assert interaction.followup.send.call_args.kwargs["content"] == (
            "No events found."
        )


class TestPreview:
    async def test_event_without_fields_renders_immediately(
        self, cog: TxEvents, chain: Chain
    ) -> None:
        interaction = make_interaction()

        await cog.preview_tx_event.callback(
            cog, interaction, contract="rocketUpgradeOneDotFour", function="execute"
        )

        [embed] = interaction.followup.send.call_args.kwargs["embeds"]
        assert embed.title == ":ringed_planet: Saturn 1 Upgrade Complete!"

    async def test_modal_parses_json_values(self, chain: Chain) -> None:
        event = TRANSACTION_REGISTRY["rocketDAOProtocolProposals"][
            "proposalSettingBool"
        ]
        modal = PreviewTxModal(
            event,
            "proposalSettingBool",
            BlockNumber(100),
            _get_event_fields(event),
        )
        values = {
            "settingContractName": "",
            "settingPath": "node.smoothing.pool.enabled",
            "value": "1",
        }
        modal.param_inputs = [
            SimpleNamespace(value=values[name])  # type: ignore[misc]
            for name, _ in modal.fields
        ]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        [embed] = interaction.followup.send.call_args.kwargs["embeds"]
        # "1" is parsed as JSON, so the bool setting renders as True; the
        # empty contract name is left out entirely
        assert embed.description == (
            "Setting `node.smoothing.pool.enabled` set to `True`!"
        )
        assert all(f.name != "Contract" for f in embed.fields)
