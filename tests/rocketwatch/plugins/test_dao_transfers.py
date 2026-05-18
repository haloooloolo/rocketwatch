from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_typing import BlockNumber

from rocketwatch.plugins.dao_transfers.dao_transfers import (
    DAOTransfers,
    _pad_address,
)
from rocketwatch.utils.embeds import Embed
from tests.lib.event_log_script import EventLogScript, make_log
from tests.lib.scripted_rocketpool import ScriptedRocketPool


class TestPadAddress:
    def test_pads_to_32_byte_hex_topic(self) -> None:
        # An address is 20 bytes; padded form is 32 bytes (64 hex chars).
        out = _pad_address("0x" + "11" * 20)
        assert out.startswith("0x")
        assert len(out) == 2 + 64
        # The original address bytes appear at the end.
        assert out.endswith("11" * 20)


_TRANSFER_TOPIC_HEX = "0x" + "dd" * 32
DAO_MULTISIG = "0x" + "AA" * 20
TOKEN_ADDR = "0x" + "55" * 20


def _padded(value: str) -> bytes:
    """20-byte address → 32-byte zero-padded topic bytes."""
    return bytes(12) + bytes.fromhex(value.removeprefix("0x"))


@pytest.fixture
def dao_transfers_cog(
    monkeypatch: pytest.MonkeyPatch,
    scripted_rp: ScriptedRocketPool,
) -> DAOTransfers:
    # Pin a real hex transfer topic for the filter — the module-level value
    # was computed against the conftest's MagicMock w3.
    from rocketwatch.plugins.dao_transfers import dao_transfers as mod

    monkeypatch.setattr(mod, "_TRANSFER_TOPIC", _TRANSFER_TOPIC_HEX)
    cog = DAOTransfers(MagicMock())
    cog._from_topics = ["0x" + "00" * 12 + DAO_MULTISIG.removeprefix("0x")]
    return cog


class TestGetPastEvents:
    async def test_returns_empty_when_no_from_topics_configured(
        self, dao_transfers_cog: DAOTransfers
    ) -> None:
        # If `cfg.rocketpool.dao_multisigs` is empty the cog short-circuits
        # before hitting the chain.
        dao_transfers_cog._from_topics = []
        result = await dao_transfers_cog.get_past_events(
            cast(BlockNumber, 0), cast(BlockNumber, 100)
        )
        assert result == []

    async def test_returns_empty_when_no_matching_logs(
        self,
        dao_transfers_cog: DAOTransfers,
        event_log_script: EventLogScript,
    ) -> None:
        # No logs scripted → no events.
        result = await dao_transfers_cog.get_past_events(
            cast(BlockNumber, 0), cast(BlockNumber, 100)
        )
        assert result == []

    async def test_builds_event_from_matching_transfer(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dao_transfers_cog: DAOTransfers,
        event_log_script: EventLogScript,
        scripted_rp: ScriptedRocketPool,
    ) -> None:
        # Stub the heavy collaborators (explorer URLs + embed builder) so the
        # test exercises the cog's decoding logic, not the embed renderer.
        monkeypatch.setattr(
            "rocketwatch.plugins.dao_transfers.dao_transfers.el_explorer_url",
            AsyncMock(side_effect=lambda addr, **_: f"[{addr}]"),
        )

        async def fake_embed(text: str, _tx_hash: str) -> Embed:
            e = Embed()
            e.description = text
            return e

        monkeypatch.setattr(
            "rocketwatch.plugins.dao_transfers.dao_transfers.build_small_event_embed",
            fake_embed,
        )

        # Stub w3.to_checksum_address so it's an identity-ish op.
        from rocketwatch.plugins.dao_transfers import dao_transfers as mod

        monkeypatch.setattr(
            mod.w3,
            "to_checksum_address",
            lambda b: "0x" + bytes(b).hex(),
            raising=False,
        )

        # Script symbol & decimals for the ERC20 contract.
        scripted_rp.set_call("ERC20.symbol", "RPL")
        scripted_rp.set_call("ERC20.decimals", 18)

        # Build a Transfer(from=DAO_MULTISIG, to=0xBBBB..., amount=1.5e18).
        recipient = "0x" + "BB" * 20
        amount_wei = (15 * 10**18) // 10  # 1.5 RPL in wei
        event_log_script.add(
            make_log(
                address=TOKEN_ADDR,
                topics=[
                    bytes.fromhex(_TRANSFER_TOPIC_HEX.removeprefix("0x")),
                    _padded(DAO_MULTISIG),
                    _padded(recipient),
                ],
                data=amount_wei.to_bytes(32, "big"),
                block_number=10,
                log_index=2,
                transaction_index=1,
                transaction_hash=b"\xab" * 32,
            )
        )

        events = await dao_transfers_cog.get_past_events(
            cast(BlockNumber, 0), cast(BlockNumber, 100)
        )

        assert len(events) == 1
        event = events[0]
        assert event.block_number == 10
        assert event.event_name == "pdao_erc20_transfer_event"
        # The unique id encodes the tx hash + log index for dedup.
        assert ":pdao_erc20_transfer:2" in event.unique_id
        # The embed description should mention the amount and token symbol.
        assert event.embed.description is not None
        assert "1.5 RPL" in event.embed.description

    async def test_filters_logs_by_from_address_topic(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dao_transfers_cog: DAOTransfers,
        event_log_script: EventLogScript,
    ) -> None:
        # A Transfer log whose `from` topic is NOT in the multisig set must
        # be excluded by the filter — the cog never even processes it.
        other_sender = "0x" + "FF" * 20

        event_log_script.add(
            make_log(
                address=TOKEN_ADDR,
                topics=[
                    bytes.fromhex(_TRANSFER_TOPIC_HEX.removeprefix("0x")),
                    _padded(other_sender),
                    _padded("0x" + "00" * 20),
                ],
                data=(10**18).to_bytes(32, "big"),
                block_number=10,
            )
        )

        events = await dao_transfers_cog.get_past_events(
            cast(BlockNumber, 0), cast(BlockNumber, 100)
        )
        assert events == []
