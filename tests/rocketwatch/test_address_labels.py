from rocketwatch.utils.address_labels import (
    _PROXY_CLASSES,
    _format_project,
    _most_attested,
    _pick_display_name,
)


def _lbl(tag_id: str, tag_value: str) -> dict:
    return {"tag_id": tag_id, "tag_value": tag_value}


class TestMostAttested:
    def test_single_entry(self):
        assert _most_attested([_lbl("contract_name", "Foo")]) == "Foo"

    def test_picks_majority(self):
        entries = [
            _lbl("contract_name", "Foo"),
            _lbl("contract_name", "Foo"),
            _lbl("contract_name", "Bar"),
        ]
        assert _most_attested(entries) == "Foo"

    def test_tie_returns_one_of_the_values(self):
        # Counter ordering on ties is implementation-defined; just assert it's a valid pick.
        entries = [
            _lbl("contract_name", "Foo"),
            _lbl("contract_name", "Bar"),
        ]
        assert _most_attested(entries) in {"Foo", "Bar"}


class TestFormatProject:
    def test_passthrough_titlecases(self):
        assert _format_project("lido") == "Lido"

    def test_known_override_circle(self):
        assert _format_project("circlefin") == "Circle"

    def test_known_override_tether(self):
        assert _format_project("tetherto") == "Tether"

    def test_multiword_slug(self):
        # Title() turns it into capitalized-words, no override needed.
        assert _format_project("rocketpool") == "Rocketpool"


class TestPickDisplayName:
    def test_empty_returns_none(self):
        assert _pick_display_name([]) is None

    def test_owner_project_only(self):
        labels = [_lbl("owner_project", "lido")]
        assert _pick_display_name(labels) == "Lido"

    def test_erc20_name_takes_precedence_over_contract_name(self):
        labels = [
            _lbl("erc20.name", "USD Coin"),
            _lbl("contract_name", "FiatTokenV2_2"),
        ]
        assert _pick_display_name(labels) == "USD Coin"

    def test_erc20_beats_erc721_when_both_present(self):
        # Loop order is erc20 → erc721 → erc1155; erc20 wins.
        labels = [
            _lbl("erc721.name", "MyNFT"),
            _lbl("erc20.name", "MyToken"),
        ]
        assert _pick_display_name(labels) == "MyToken"

    def test_proxy_classes_skipped_in_contract_name(self):
        labels = [
            _lbl("contract_name", "TransparentUpgradeableProxy"),
            _lbl("contract_name", "RocketStorage"),
        ]
        assert _pick_display_name(labels) == "RocketStorage"

    def test_all_proxies_falls_back_to_proxy(self):
        # If every contract_name is a proxy class, we still pick one rather than returning None.
        labels = [_lbl("contract_name", "ERC1967Proxy")]
        assert _pick_display_name(labels) == "ERC1967Proxy"

    def test_project_and_specific_combined(self):
        labels = [
            _lbl("owner_project", "rocketpool"),
            _lbl("contract_name", "RocketDepositPool"),
        ]
        assert _pick_display_name(labels) == "Rocketpool: RocketDepositPool"

    def test_project_substring_of_specific_dedupes(self):
        # If the project name is already contained in the specific name, don't repeat it.
        labels = [
            _lbl("owner_project", "lido"),
            _lbl("contract_name", "LidoStakedETH"),
        ]
        assert _pick_display_name(labels) == "LidoStakedETH"

    def test_only_project_when_no_specific(self):
        assert _pick_display_name([_lbl("owner_project", "lido")]) == "Lido"

    def test_unknown_tags_ignored(self):
        labels = [_lbl("random_tag", "ignored")]
        assert _pick_display_name(labels) is None

    def test_erc1155_used_when_no_erc20_or_erc721(self):
        labels = [_lbl("erc1155.name", "MultiToken")]
        assert _pick_display_name(labels) == "MultiToken"


class TestProxyClasses:
    def test_known_proxy_names_present(self):
        # If someone renames the constants, these tests will catch it.
        assert "TransparentUpgradeableProxy" in _PROXY_CLASSES
        assert "ERC1967Proxy" in _PROXY_CLASSES
        assert "Proxy" in _PROXY_CLASSES
