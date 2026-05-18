from collections.abc import Iterator
from typing import Annotated, NotRequired, Required, TypedDict

import pytest
from eth_typing import ChecksumAddress

from rocketwatch.utils import type_markers
from rocketwatch.utils.type_markers import (
    ContractAddress,
    NodeAddress,
    Percentage,
    TWei,
    Wei,
    _ContractAddress,
    _get_marker,
    _NodeAddress,
    _Percentage,
    _Wei,
    auto_format,
)

ETH = 10**18
GWEI = 10**9


@pytest.fixture(autouse=True)
def _stub_address_helpers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # `_addr` calls sea_creature lookup + el_explorer_url, both of which talk
    # to rp/w3. Replace with deterministic stubs so tests focus on dispatch.
    async def fake_addr(address: ChecksumAddress) -> str:
        return f"<addr:{address}>"

    monkeypatch.setattr(type_markers, "_addr", fake_addr)
    yield


class _SimpleArgs(TypedDict):
    bond: Wei
    fee_gwei: TWei
    rate: Percentage
    node: NodeAddress
    contract: ContractAddress
    untouched: str


class TestGetMarker:
    def test_returns_marker_for_annotated_int(self) -> None:
        assert isinstance(_get_marker(Wei), _Wei)

    def test_returns_marker_for_annotated_address(self) -> None:
        assert isinstance(_get_marker(NodeAddress), _NodeAddress)

    def test_strips_notrequired_wrapper(self) -> None:
        # TypedDict NotRequired/Required wrap the inner annotated type — the
        # marker lookup must see through that.
        assert isinstance(_get_marker(NotRequired[ContractAddress]), _ContractAddress)
        assert isinstance(_get_marker(Required[Percentage]), _Percentage)

    def test_returns_none_for_plain_type(self) -> None:
        assert _get_marker(int) is None

    def test_returns_none_for_annotated_without_marker(self) -> None:
        assert _get_marker(Annotated[int, "some unrelated metadata"]) is None


class TestAutoFormat:
    async def test_converts_wei_and_percentage(self) -> None:
        out = await auto_format(
            {
                "bond": 4 * ETH,
                "fee_gwei": 5 * 10**6,
                "rate": 5 * 10**16,
                "untouched": "hello",
            },
            _SimpleArgs,
        )
        assert out["bond"] == pytest.approx(4.0)
        # TWei has decimals=6.
        assert out["fee_gwei"] == pytest.approx(5.0)
        # Percentage scales by 100 after the to_float conversion.
        assert out["rate"] == pytest.approx(5.0)
        # Non-annotated keys pass through unchanged.
        assert out["untouched"] == "hello"

    async def test_resolves_addresses_via_addr_helper(self) -> None:
        out = await auto_format(
            {
                "node": "0xNODE",
                "contract": "0xCON",
            },
            _SimpleArgs,
        )
        assert out["node"] == "<addr:0xNODE>"
        assert out["contract"] == "<addr:0xCON>"

    async def test_missing_keys_are_skipped(self) -> None:
        # Only the keys present in args get processed.
        out = await auto_format({"bond": ETH}, _SimpleArgs)
        assert set(out.keys()) == {"bond"}
        assert out["bond"] == pytest.approx(1.0)

    async def test_returns_new_dict_does_not_mutate_input(self) -> None:
        original: dict[str, int] = {"bond": ETH}
        out = await auto_format(original, _SimpleArgs)
        assert original == {"bond": ETH}
        assert out["bond"] == pytest.approx(1.0)

    async def test_address_resolution_runs_concurrently(self) -> None:
        # Two address fields → two coros gathered. Verify both end up in the
        # output (the dispatch logic depends on the gather succeeding for all).
        out = await auto_format(
            {"node": "0xA", "contract": "0xB", "bond": ETH},
            _SimpleArgs,
        )
        assert out["node"] == "<addr:0xA>"
        assert out["contract"] == "<addr:0xB>"
        assert out["bond"] == pytest.approx(1.0)
