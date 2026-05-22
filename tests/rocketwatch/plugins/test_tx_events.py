from typing import Any
from unittest.mock import AsyncMock

import pytest
from hexbytes import HexBytes

from rocketwatch.plugins.tx_events.event_definitions import TRANSACTION_REGISTRY
from rocketwatch.plugins.tx_events.tx_events import TxEvents, _get_event_fields
from rocketwatch.utils.embeds import Embed
from tests.lib.discord_harness import make_bot, make_interaction
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
