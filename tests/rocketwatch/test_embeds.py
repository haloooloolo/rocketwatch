from unittest.mock import AsyncMock, MagicMock

import pytest
from discord import Color
from ens import InvalidName

from rocketwatch.utils import embeds
from rocketwatch.utils.embeds import (
    CustomColors,
    Embed,
    build_event_embed,
    build_rich_event_embed,
    build_small_event_embed,
    el_explorer_url,
    format_value,
    resolve_ens,
)


class TestFormatValue:
    def test_zero(self):
        # Zero is special-cased (log10 would blow up); should return "0".
        assert format_value(0) == "0"

    def test_small_integer(self):
        assert format_value(5) == "5"

    def test_thousands_get_commas(self):
        assert format_value(1234567) == "1,234,567"

    def test_small_floats_keep_meaningful_precision(self):
        # A tiny value should not be rounded to "0".
        out = format_value(0.00012345678)
        assert out.startswith("0.0001")
        assert out != "0"

    def test_trailing_integer_drops_decimal(self):
        # Whole-number floats should display without a trailing ".0".
        assert format_value(5.0) == "5"

    def test_negative_values_format_with_sign(self):
        # Negative values must be supported and round-trip the sign.
        out = format_value(-1234)
        assert out.startswith("-")
        assert "1,234" in out

    def test_large_floats_drop_excess_decimals(self):
        # For values whose integer part already has 6 digits, sub-integer noise
        # should not appear in the output — there's no useful precision left.
        out = format_value(123456.789)
        assert "123,456" in out or "123,457" in out
        assert "." not in out


class TestCustomColors:
    def test_colors_are_discord_color_instances(self):
        assert isinstance(CustomColors.RED, Color)
        assert isinstance(CustomColors.ORANGE, Color)
        assert isinstance(CustomColors.YELLOW, Color)
        assert isinstance(CustomColors.GREEN, Color)


class TestEmbedFooter:
    def test_default_color_is_orange(self, mainnet_cfg):
        e = Embed()
        assert e.color == CustomColors.ORANGE

    def test_explicit_color_overrides_default(self, mainnet_cfg):
        e = Embed(color=CustomColors.RED)
        assert e.color == CustomColors.RED

    def test_mainnet_footer_omits_chain(self, mainnet_cfg):
        e = Embed()
        assert e.footer.text is not None
        assert "Chain:" not in e.footer.text
        assert "Created by" in e.footer.text

    def test_testnet_footer_includes_chain(self, testnet_cfg):
        e = Embed()
        assert e.footer.text is not None
        assert "Chain: Holesky" in e.footer.text

    def test_set_footer_parts_appends_to_base(self, mainnet_cfg):
        e = Embed()
        e.set_footer_parts(["Block 123", "Synced"])
        assert e.footer.text is not None
        assert "Block 123" in e.footer.text
        assert "Synced" in e.footer.text
        # The base "Created by ..." prefix is preserved.
        assert e.footer.text.startswith("Created by")

    def test_set_footer_parts_replaces_previous(self, mainnet_cfg):
        e = Embed()
        e.set_footer_parts(["A"])
        e.set_footer_parts(["B"])
        assert e.footer.text is not None
        assert "A" not in e.footer.text
        assert "B" in e.footer.text


# ---- shared mocks for async embed builders ---------------------------------


@pytest.fixture
def stub_explorer(monkeypatch):
    """Replace el_explorer_url with a recording stub that returns a sentinel link."""

    async def fake_url(target, name="", prefix=None, name_fmt=None, block="latest"):
        # Mirror the real shape "{prefix}[{name}]({url})" closely enough that
        # tests can find the target inside the link. The real function never
        # wraps the prefix in parens, so we don't either — keeps the
        # "caller (sender)" grouping syntax in build_rich_event_embed unambiguous.
        p = prefix or ""
        return f"{p}[{name or target}](explorer/{target})"

    monkeypatch.setattr(embeds, "el_explorer_url", fake_url)
    return fake_url


@pytest.fixture
def stub_block_to_ts(monkeypatch):
    monkeypatch.setattr(embeds, "block_to_ts", AsyncMock(return_value=1_700_000_000))


def _field(embed, name):
    """Return the value of the first field with the given name (or None)."""
    for f in embed.fields:
        if f.name == name:
            return f.value
    return None


class TestBuildSmallEventEmbed:
    async def test_includes_description_and_tx_link(self, mainnet_cfg, stub_explorer):
        e = await build_small_event_embed("Something happened", "0xabc")
        assert "Something happened" in e.description
        # The explorer stub embeds the target — anything resembling a link to
        # the tx hash should appear in the description.
        assert "0xabc" in e.description

    async def test_mainnet_omits_chain_suffix(self, mainnet_cfg, stub_explorer):
        e = await build_small_event_embed("Hi", "0xabc")
        assert "(Mainnet)" not in (e.description or "")

    async def test_non_mainnet_appends_chain(self, testnet_cfg, stub_explorer):
        e = await build_small_event_embed("Hi", "0xabc")
        assert "(Holesky)" in (e.description or "")

    async def test_footer_is_empty(self, mainnet_cfg, stub_explorer):
        # Small embeds intentionally clear the default footer.
        e = await build_small_event_embed("Hi", "0xabc")
        assert (e.footer.text or "") == ""


class TestBuildEventEmbed:
    async def test_adds_provided_fields(
        self, mainnet_cfg, stub_explorer, stub_block_to_ts
    ):
        fields = [("Amount", "100 ETH", True), ("Recipient", "0xabc", False)]
        e = await build_event_embed(tx_hash="0xdead", block_number=1234, fields=fields)
        assert _field(e, "Amount") == "100 ETH"
        assert _field(e, "Recipient") == "0xabc"

    async def test_always_has_tx_block_timestamp(
        self, mainnet_cfg, stub_explorer, stub_block_to_ts
    ):
        e = await build_event_embed(tx_hash="0xdead", block_number=1234)
        assert _field(e, "Transaction Hash") is not None
        assert _field(e, "Block Number") is not None
        assert _field(e, "Timestamp") is not None

    async def test_block_number_links_to_explorer(
        self, mainnet_cfg, stub_explorer, stub_block_to_ts
    ):
        e = await build_event_embed(tx_hash="0xdead", block_number=42)
        # The block field should reference the configured explorer and the block.
        block_field = _field(e, "Block Number")
        assert block_field is not None
        assert "42" in block_field
        assert "etherscan.io" in block_field

    async def test_missing_0x_prefix_is_added(
        self, mainnet_cfg, stub_explorer, stub_block_to_ts
    ):
        # Spec: the function normalises bare hex by prepending 0x before linking.
        await build_event_embed(tx_hash="dead", block_number=1)
        # The stub embeds the *normalised* target into its return value.
        # Inspect the resulting field text to confirm normalisation occurred.
        e = await build_event_embed(tx_hash="dead", block_number=1)
        tx_field = _field(e, "Transaction Hash")
        assert tx_field is not None
        assert "0xdead" in tx_field

    async def test_extra_kwargs_passed_to_embed(
        self, mainnet_cfg, stub_explorer, stub_block_to_ts
    ):
        e = await build_event_embed(tx_hash="0xdead", block_number=1, title="My Title")
        assert e.title == "My Title"


class TestBuildRichEventEmbed:
    @pytest.fixture
    def stub_sea_creature(self, monkeypatch):
        monkeypatch.setattr(
            embeds,
            "get_sea_creature_for_address",
            AsyncMock(return_value="🐳"),
        )

    @pytest.fixture
    def stub_w3_checksum(self, monkeypatch):
        # to_checksum_address is called twice in the sender/caller branch;
        # for testing the rest of the function it can just echo the input.
        monkeypatch.setattr(embeds.w3, "to_checksum_address", lambda a: a)

    async def test_no_sender_means_no_sender_field(
        self, mainnet_cfg, stub_explorer, stub_block_to_ts
    ):
        e = await build_rich_event_embed(tx_hash="0xabc", block_number=1)
        assert _field(e, "Sender Address") is None

    async def test_sender_only_shows_single_link(
        self,
        mainnet_cfg,
        stub_explorer,
        stub_block_to_ts,
        stub_sea_creature,
        stub_w3_checksum,
    ):
        e = await build_rich_event_embed(tx_hash="0xabc", block_number=1, sender="0xS")
        sender_field = _field(e, "Sender Address")
        assert sender_field is not None
        # Sender-only renders as a single markdown link, so exactly one "[…](…)" pair.
        assert sender_field.count("](") == 1

    async def test_caller_distinct_from_sender_shown_as_caller_then_sender(
        self,
        mainnet_cfg,
        stub_explorer,
        stub_block_to_ts,
        stub_sea_creature,
        stub_w3_checksum,
    ):
        e = await build_rich_event_embed(
            tx_hash="0xabc",
            block_number=1,
            sender="0xS",
            caller="0xC",
        )
        sender_field = _field(e, "Sender Address")
        assert sender_field is not None
        # Both addresses should appear, and the field should contain two markdown links.
        assert "0xC" in sender_field
        assert "0xS" in sender_field
        assert sender_field.count("](") == 2

    async def test_caller_equal_to_sender_collapsed(
        self,
        mainnet_cfg,
        stub_explorer,
        stub_block_to_ts,
        stub_sea_creature,
        stub_w3_checksum,
    ):
        # If caller and sender are the same address, only one link should appear.
        e = await build_rich_event_embed(
            tx_hash="0xabc",
            block_number=1,
            sender="0xS",
            caller="0xS",
        )
        sender_field = _field(e, "Sender Address")
        assert sender_field is not None
        assert sender_field.count("](") == 1

    async def test_caller_zero_address_collapsed(
        self,
        mainnet_cfg,
        stub_explorer,
        stub_block_to_ts,
        stub_sea_creature,
        stub_w3_checksum,
    ):
        # Zero-address caller (typical for system-originated txs) is treated as
        # "no separate caller" and collapses to a single link.
        from web3.constants import ADDRESS_ZERO

        e = await build_rich_event_embed(
            tx_hash="0xabc",
            block_number=1,
            sender="0xS",
            caller=ADDRESS_ZERO,
        )
        sender_field = _field(e, "Sender Address")
        assert sender_field is not None
        assert sender_field.count("](") == 1

    async def test_no_receipt_means_no_fee_field(
        self,
        mainnet_cfg,
        stub_explorer,
        stub_block_to_ts,
    ):
        e = await build_rich_event_embed(tx_hash="0xabc", block_number=1)
        assert _field(e, "Transaction Fee") is None

    async def test_fee_in_wei_for_small_amounts(
        self,
        mainnet_cfg,
        stub_explorer,
        stub_block_to_ts,
        monkeypatch,
    ):
        # gasUsed * effectiveGasPrice < 1e9 → Wei unit.
        monkeypatch.setattr(embeds.rp, "get_eth_usdc_price", AsyncMock(return_value=0))
        receipt = {"gasUsed": 100, "effectiveGasPrice": 1}  # 100 wei
        e = await build_rich_event_embed(
            tx_hash="0xabc", block_number=1, receipt=receipt
        )
        fee = _field(e, "Transaction Fee")
        assert fee is not None
        assert "Wei" in fee
        assert "Gwei" not in fee
        assert "ETH" not in fee

    async def test_fee_in_gwei_for_mid_amounts(
        self,
        mainnet_cfg,
        stub_explorer,
        stub_block_to_ts,
        monkeypatch,
    ):
        # 1e9 ≤ fee < 1e15 → Gwei.
        monkeypatch.setattr(embeds.rp, "get_eth_usdc_price", AsyncMock(return_value=0))
        receipt = {"gasUsed": 21_000, "effectiveGasPrice": 10**9}  # 2.1e13 wei
        e = await build_rich_event_embed(
            tx_hash="0xabc", block_number=1, receipt=receipt
        )
        fee = _field(e, "Transaction Fee")
        assert fee is not None
        assert "Gwei" in fee

    async def test_fee_in_eth_for_large_amounts(
        self,
        mainnet_cfg,
        stub_explorer,
        stub_block_to_ts,
        monkeypatch,
    ):
        # fee ≥ 1e15 → ETH.
        monkeypatch.setattr(embeds.rp, "get_eth_usdc_price", AsyncMock(return_value=0))
        receipt = {"gasUsed": 10**9, "effectiveGasPrice": 10**9}  # 1e18 wei = 1 ETH
        e = await build_rich_event_embed(
            tx_hash="0xabc", block_number=1, receipt=receipt
        )
        fee = _field(e, "Transaction Fee")
        assert fee is not None
        assert "ETH" in fee

    async def test_usdc_suffix_only_on_mainnet(
        self,
        testnet_cfg,
        stub_explorer,
        stub_block_to_ts,
        monkeypatch,
    ):
        # On non-mainnet chains, USDC pricing isn't appended even with a receipt.
        monkeypatch.setattr(
            embeds.rp, "get_eth_usdc_price", AsyncMock(return_value=2000)
        )
        receipt = {"gasUsed": 10**9, "effectiveGasPrice": 10**9}
        e = await build_rich_event_embed(
            tx_hash="0xabc", block_number=1, receipt=receipt
        )
        fee = _field(e, "Transaction Fee")
        assert fee is not None
        assert "USDC" not in fee


# ---- resolve_ens ------------------------------------------------------------


@pytest.fixture
def fake_ens_module(monkeypatch):
    """Replace ``resolve_name`` and ``get_name`` on the real ens module.

    Patching the functions in-place is more robust than swapping the module
    via ``sys.modules`` — the latter doesn't survive Python's ``from X import Y``
    semantics across tests once ``rocketwatch.utils.ens`` has been imported
    elsewhere (e.g. by the plugin smoke test).
    """
    from types import SimpleNamespace

    import rocketwatch.utils.ens as ens_module

    resolve = AsyncMock(return_value=None)
    get_name = AsyncMock(return_value=None)
    monkeypatch.setattr(ens_module, "resolve_name", resolve)
    monkeypatch.setattr(ens_module, "get_name", get_name)
    return SimpleNamespace(resolve_name=resolve, get_name=get_name)


@pytest.fixture
def interaction():
    mock = MagicMock()
    mock.followup.send = AsyncMock()
    return mock


class TestResolveEns:
    async def test_ens_name_resolved_returns_input_and_address(
        self, interaction, fake_ens_module
    ):
        fake_ens_module.resolve_name.return_value = "0xABCDEF"
        name, addr = await resolve_ens(interaction, "rocketpool.eth")
        # Spec: ENS-style inputs are returned verbatim as the display name.
        assert name == "rocketpool.eth"
        assert addr == "0xABCDEF"
        interaction.followup.send.assert_not_called()

    async def test_ens_name_not_found_returns_none_and_messages_user(
        self, interaction, fake_ens_module
    ):
        fake_ens_module.resolve_name.return_value = None
        name, addr = await resolve_ens(interaction, "nope.eth")
        assert name is None
        assert addr is None
        interaction.followup.send.assert_awaited_once()

    async def test_invalid_ens_name_returns_none_and_messages_user(
        self, interaction, fake_ens_module
    ):
        fake_ens_module.resolve_name.side_effect = InvalidName("bad")
        name, addr = await resolve_ens(interaction, "bad name.eth")
        assert name is None
        assert addr is None
        interaction.followup.send.assert_awaited_once()

    async def test_invalid_address_returns_none_and_messages_user(
        self, interaction, fake_ens_module, monkeypatch
    ):
        def reject(_):
            raise ValueError("not an address")

        monkeypatch.setattr(embeds.w3, "to_checksum_address", reject)
        name, addr = await resolve_ens(interaction, "definitely_not_an_address")
        assert name is None
        assert addr is None
        interaction.followup.send.assert_awaited_once()

    async def test_address_with_reverse_record_uses_ens_name(
        self, interaction, fake_ens_module, monkeypatch
    ):
        monkeypatch.setattr(embeds.w3, "to_checksum_address", lambda a: "0xCHECKSUMMED")
        fake_ens_module.get_name.return_value = "vitalik.eth"
        name, addr = await resolve_ens(interaction, "0xabc")
        assert name == "vitalik.eth"
        assert addr == "0xCHECKSUMMED"

    async def test_address_without_reverse_record_falls_back_to_address(
        self, interaction, fake_ens_module, monkeypatch
    ):
        monkeypatch.setattr(embeds.w3, "to_checksum_address", lambda a: "0xCHECKSUMMED")
        fake_ens_module.get_name.return_value = None
        name, addr = await resolve_ens(interaction, "0xabc")
        # When no reverse record exists, display the checksum address instead.
        assert name == "0xCHECKSUMMED"
        assert addr == "0xCHECKSUMMED"


# ---- el_explorer_url --------------------------------------------------------


@pytest.fixture
def explorer_mocks(monkeypatch, fake_ens_module):
    """Default 'plain external address' mocks for el_explorer_url.

    Tests override individual attributes to exercise specific branches:
    - ``rp_call`` is an AsyncMock; tests can set ``side_effect`` to a dict-lookup
      function that returns values keyed by the ``rocket*.method`` string.
    - ``rp.is_node`` / ``is_megapool`` / ``is_minipool`` default to False.
    - ``get_address_name`` / ``get_pdao_delegates`` / ENS reverse default to empty.
    - ``w3.eth.get_code`` defaults to b"" so the contract-name branch is skipped.
    """

    monkeypatch.setattr(embeds.w3, "is_address", lambda a: True)
    monkeypatch.setattr(embeds.w3, "to_checksum_address", lambda a: a)
    monkeypatch.setattr(embeds.w3.eth, "get_code", AsyncMock(return_value=b""))

    monkeypatch.setattr(embeds.rp, "is_node", AsyncMock(return_value=False))
    monkeypatch.setattr(embeds.rp, "is_megapool", AsyncMock(return_value=False))
    monkeypatch.setattr(embeds.rp, "is_minipool", AsyncMock(return_value=False))

    # Endpoint-specific defaults: empty string for member-ID lookups,
    # False for boolean state, ADDRESS_ZERO only for address-returning calls.
    rp_call = AsyncMock(side_effect=_rp_call_responder({}))
    monkeypatch.setattr(embeds.rp, "call", rp_call)

    monkeypatch.setattr(embeds, "get_address_name", AsyncMock(return_value=None))
    monkeypatch.setattr(embeds, "get_pdao_delegates", AsyncMock(return_value={}))

    from types import SimpleNamespace

    return SimpleNamespace(
        rp_call=rp_call,
        ens=fake_ens_module,
        get_address_name=embeds.get_address_name,
        get_pdao_delegates=embeds.get_pdao_delegates,
    )


def _rp_call_responder(table):
    """Return an AsyncMock side_effect that dispatches on the contract.method key.

    Endpoint-specific defaults matter: ADDRESS_ZERO is a non-empty string and
    therefore truthy, which would otherwise spuriously trigger every member-id
    or boolean check inside ``el_explorer_url``.
    """
    from web3.constants import ADDRESS_ZERO

    defaults = {
        "rocketNodeManager.getMegapoolAddress": ADDRESS_ZERO,
        "rocketNodeManager.getSmoothingPoolRegistrationState": False,
        "rocketDAONodeTrusted.getMemberID": "",
        "rocketDAOSecurity.getMemberID": "",
    }

    async def fn(method, *args, **kwargs):
        if method in table:
            return table[method]
        return defaults.get(method, ADDRESS_ZERO)

    return fn


class TestElExplorerUrlOutputShape:
    async def test_output_uses_markdown_link_syntax(self, mainnet_cfg, explorer_mocks):
        out = await el_explorer_url("0xabc", name="Foo")
        # The function always returns "{prefix}[{name}]({url})".
        assert "[Foo]" in out
        assert "](" in out
        assert out.endswith(")")

    async def test_custom_prefix_is_prepended(self, mainnet_cfg, explorer_mocks):
        out = await el_explorer_url("0xabc", name="Foo", prefix="X ")
        assert out.startswith("X ")

    async def test_prefix_none_yields_no_prefix(self, mainnet_cfg, explorer_mocks):
        # Spec: passing prefix=None overrides the default empty-string and
        # suppresses any detected prefix (e.g. role badges).
        explorer_mocks.rp_call.side_effect = _rp_call_responder(
            {"rocketDAONodeTrusted.getMemberID": "TrustedNode42"}
        )
        from unittest.mock import AsyncMock as _AM

        embeds.rp.is_node = _AM(return_value=True)
        out = await el_explorer_url("0xabc", prefix=None)
        # No role emoji should leak through to the output.
        assert "🔮" not in out

    async def test_name_fmt_applied_to_name(self, mainnet_cfg, explorer_mocks):
        out = await el_explorer_url(
            "0xabc", name="vitalik", name_fmt=lambda s: s.upper()
        )
        assert "[VITALIK]" in out


class TestElExplorerUrlTransactionHash:
    async def test_non_address_target_uses_tx_path(self, mainnet_cfg, monkeypatch):
        # When w3.is_address returns False we treat the input as a tx hash and
        # build a /tx/<hash> URL — no RP lookups should run.
        monkeypatch.setattr(embeds.w3, "is_address", lambda a: False)
        out = await el_explorer_url("0xdeadbeef", name="[txn]")
        assert "/tx/0xdeadbeef" in out
        assert "/address/" not in out

    async def test_no_name_uses_shortened_hash_fallback(self, mainnet_cfg, monkeypatch):
        monkeypatch.setattr(embeds.w3, "is_address", lambda a: False)
        out = await el_explorer_url("0xdeadbeefcafebabe")
        # s_hex truncates to the first 10 chars.
        assert "[0xdeadbeef]" in out


class TestElExplorerUrlPlainAddress:
    async def test_external_address_links_to_address_path(
        self, mainnet_cfg, explorer_mocks
    ):
        out = await el_explorer_url("0xabc", name="Foo")
        assert "/address/0xabc" in out

    async def test_falls_back_to_address_label(self, mainnet_cfg, explorer_mocks):
        explorer_mocks.get_address_name.return_value = "Etherscan: Some Label"
        out = await el_explorer_url("0xabc")
        assert "[Etherscan: Some Label]" in out

    async def test_falls_back_to_ens_reverse_record(self, mainnet_cfg, explorer_mocks):
        explorer_mocks.ens.get_name.return_value = "vitalik.eth"
        out = await el_explorer_url("0xabc")
        assert "[vitalik.eth]" in out

    async def test_address_label_preferred_over_ens(self, mainnet_cfg, explorer_mocks):
        # Spec: hand-curated / OLI labels win over ENS reverse records.
        explorer_mocks.get_address_name.return_value = "Curated Label"
        explorer_mocks.ens.get_name.return_value = "ens-name.eth"
        out = await el_explorer_url("0xabc")
        assert "[Curated Label]" in out
        assert "ens-name.eth" not in out

    async def test_no_name_anywhere_falls_back_to_short_hex(
        self, mainnet_cfg, explorer_mocks
    ):
        out = await el_explorer_url("0xdeadbeef123456")
        assert "[0xdeadbeef]" in out

    async def test_non_mainnet_defaults_to_short_hex_before_lookups(
        self, testnet_cfg, explorer_mocks
    ):
        # Spec: on non-mainnet chains the function short-circuits naming with
        # s_hex rather than hitting label/ENS lookups.
        explorer_mocks.get_address_name.return_value = "Should Not Win"
        explorer_mocks.ens.get_name.return_value = "should-not-win.eth"
        out = await el_explorer_url("0xdeadbeef123456")
        assert "[0xdeadbeef]" in out
        assert "Should Not Win" not in out


class TestElExplorerUrlContractCode:
    async def test_contract_address_gets_doc_prefix(
        self, mainnet_cfg, explorer_mocks, monkeypatch
    ):
        # When the address has bytecode, the function flags it with a 📄 prefix.
        monkeypatch.setattr(
            embeds.w3.eth, "get_code", AsyncMock(return_value=b"\x60\x80")
        )
        out = await el_explorer_url("0xabc", name="MyContract")
        assert "📄" in out


class TestElExplorerUrlNode:
    async def test_node_smoothing_pool_adds_cup_prefix(
        self, mainnet_cfg, explorer_mocks
    ):
        embeds.rp.is_node = AsyncMock(return_value=True)
        explorer_mocks.rp_call.side_effect = _rp_call_responder(
            {"rocketNodeManager.getSmoothingPoolRegistrationState": True}
        )
        out = await el_explorer_url("0xabc", name="Node")
        assert ":cup_with_straw:" in out

    async def test_odao_member_uses_member_id_and_orb_prefix(
        self, mainnet_cfg, explorer_mocks
    ):
        embeds.rp.is_node = AsyncMock(return_value=True)
        explorer_mocks.rp_call.side_effect = _rp_call_responder(
            {"rocketDAONodeTrusted.getMemberID": "TrustedNode42"}
        )
        out = await el_explorer_url("0xabc")
        assert "🔮" in out
        assert "[TrustedNode42]" in out

    async def test_sdao_member_uses_member_id_and_lock_prefix(
        self, mainnet_cfg, explorer_mocks
    ):
        embeds.rp.is_node = AsyncMock(return_value=True)
        explorer_mocks.rp_call.side_effect = _rp_call_responder(
            {"rocketDAOSecurity.getMemberID": "SecurityCouncilist"}
        )
        out = await el_explorer_url("0xabc")
        assert "🔒" in out
        assert "[SecurityCouncilist]" in out

    async def test_odao_wins_over_sdao(self, mainnet_cfg, explorer_mocks):
        # Spec: the dispatch is elif-chained so oDAO membership shadows sDAO
        # — useful when a single node holds both roles.
        embeds.rp.is_node = AsyncMock(return_value=True)
        explorer_mocks.rp_call.side_effect = _rp_call_responder(
            {
                "rocketDAONodeTrusted.getMemberID": "OdaoName",
                "rocketDAOSecurity.getMemberID": "SdaoName",
            }
        )
        out = await el_explorer_url("0xabc")
        assert "🔮" in out
        assert "🔒" not in out
        assert "[OdaoName]" in out

    async def test_pdao_delegate_uses_delegate_name(self, mainnet_cfg, explorer_mocks):
        embeds.rp.is_node = AsyncMock(return_value=True)
        explorer_mocks.get_pdao_delegates.return_value = {"0xabc": "delegate.eth"}
        out = await el_explorer_url("0xabc")
        assert "🏛️" in out
        assert "[delegate.eth]" in out

    async def test_node_with_megapool_redirects_to_rocketdash(
        self, mainnet_cfg, explorer_mocks
    ):
        embeds.rp.is_node = AsyncMock(return_value=True)
        explorer_mocks.rp_call.side_effect = _rp_call_responder(
            {"rocketNodeManager.getMegapoolAddress": "0xMEGA"}
        )
        out = await el_explorer_url("0xabc", name="Node")
        assert "rocketdash.net/megapool/0xMEGA" in out


class TestElExplorerUrlMegapoolAndMinipool:
    async def test_megapool_redirects_to_rocketdash(self, mainnet_cfg, explorer_mocks):
        embeds.rp.is_megapool = AsyncMock(return_value=True)
        out = await el_explorer_url("0xMEGA", name="MP")
        assert "rocketdash.net/megapool/0xMEGA" in out

    async def test_megapool_appends_network_query_on_testnet(
        self, testnet_cfg, explorer_mocks
    ):
        embeds.rp.is_megapool = AsyncMock(return_value=True)
        out = await el_explorer_url("0xMEGA", name="MP")
        # Non-mainnet chains attach ?network=<chain> to dashboard URLs.
        assert "?network=holesky" in out

    async def test_megapool_mainnet_has_no_network_query(
        self, mainnet_cfg, explorer_mocks
    ):
        embeds.rp.is_megapool = AsyncMock(return_value=True)
        out = await el_explorer_url("0xMEGA", name="MP")
        assert "?network=" not in out

    async def test_minipool_mainnet_uses_rocketexplorer(
        self, mainnet_cfg, explorer_mocks
    ):
        embeds.rp.is_minipool = AsyncMock(return_value=True)
        out = await el_explorer_url("0xMINI", name="MP")
        assert "rocketexplorer.net/validator/0xMINI" in out

    async def test_minipool_non_mainnet_keeps_address_url(
        self, testnet_cfg, explorer_mocks
    ):
        # Spec: rocketexplorer.net is mainnet-only; testnet minipools fall back
        # to the chain's normal address explorer URL.
        embeds.rp.is_minipool = AsyncMock(return_value=True)
        out = await el_explorer_url("0xMINI", name="MP")
        assert "rocketexplorer.net" not in out
        assert "/address/0xMINI" in out
