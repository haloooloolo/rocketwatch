from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from rocketwatch.plugins.random import random as random_module
from rocketwatch.plugins.random.random import Random
from tests.lib.discord_harness import make_bot, make_interaction
from tests.lib.scripted_rocketpool import ScriptedRocketPool, addr

ETH = 10**18


class _FakeResp:
    def __init__(self, json_data: Any) -> None:
        self._json = json_data

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def json(self) -> Any:
        return self._json


class _FakeSession:
    def __init__(self, json_data: Any) -> None:
        self._json = json_data

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    def get(self, *_a: Any, **_k: Any) -> _FakeResp:
        return _FakeResp(self._json)


def _patch_http(monkeypatch: pytest.MonkeyPatch, json_data: Any) -> None:
    monkeypatch.setattr(
        random_module.aiohttp,
        "ClientSession",
        lambda *a, **k: _FakeSession(json_data),
    )


class TestRestaurantNames:
    async def test_mexican_name_is_sent(self) -> None:
        cog = Random(make_bot())
        interaction = make_interaction()
        await cog.mexican_restaurant_name.callback(cog, interaction)
        interaction.response.send_message.assert_awaited_once()
        assert isinstance(interaction.response.send_message.call_args.args[0], str)

    async def test_austrian_name_is_sent(self) -> None:
        cog = Random(make_bot())
        interaction = make_interaction()
        await cog.austrian_restaurant_name.callback(cog, interaction)
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.call_args.args[0]


class TestOnReady:
    async def test_populates_contract_names_once(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        scripted_rp.set_address("rocketDepositPool", addr("0x" + "11" * 20))
        scripted_rp.set_address("rocketTokenRPL", addr("0x" + "22" * 20))
        cog = Random(make_bot())
        await cog.on_ready()
        assert set(cog.contract_names) == {"rocketDepositPool", "rocketTokenRPL"}
        # Second call is a no-op (already populated).
        scripted_rp.set_address("rocketExtra", addr("0x" + "33" * 20))
        await cog.on_ready()
        assert "rocketExtra" not in cog.contract_names


class TestMatchContractNames:
    async def test_case_insensitive_substring_filter(self) -> None:
        cog = Random(make_bot())
        cog.contract_names = ["rocketTokenRPL", "rocketTokenRETH", "rocketDepositPool"]
        out = await cog.match_contract_names(make_interaction(), "token")
        assert {c.value for c in out} == {"rocketTokenRPL", "rocketTokenRETH"}

    async def test_caps_at_25(self) -> None:
        cog = Random(make_bot())
        cog.contract_names = [f"rocketThing{i}" for i in range(40)]
        out = await cog.match_contract_names(make_interaction(), "thing")
        assert len(out) == 25


class TestDevTime:
    async def test_sends_embed_with_beacon_time(self, scripted_bacon: Any) -> None:
        scripted_bacon.set_block_header("head", {"slot": "1000"})
        cog = Random(make_bot())
        interaction = make_interaction()
        await cog.dev_time.callback(cog, interaction)
        interaction.response.send_message.assert_awaited_once()
        embed = interaction.response.send_message.call_args.kwargs["embed"]
        field_names = [f.name for f in embed.fields]
        assert "Beacon Time" in field_names


class TestGetBlockByTimestamp:
    async def test_perfect_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(random_module, "ts_to_block", AsyncMock(return_value=123))
        monkeypatch.setattr(
            random_module, "block_to_ts", AsyncMock(return_value=1_700_000_000)
        )
        cog = Random(make_bot())
        interaction = make_interaction()
        await cog.get_block_by_timestamp.callback(
            cog, interaction, timestamp=1_700_000_000
        )
        content = interaction.followup.send.call_args.kwargs["content"]
        assert "perfect match" in content
        assert "123" in content

    async def test_close_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(random_module, "ts_to_block", AsyncMock(return_value=123))
        monkeypatch.setattr(
            random_module, "block_to_ts", AsyncMock(return_value=1_700_000_005)
        )
        cog = Random(make_bot())
        interaction = make_interaction()
        await cog.get_block_by_timestamp.callback(
            cog, interaction, timestamp=1_700_000_000
        )
        content = interaction.followup.send.call_args.kwargs["content"]
        assert "close match" in content


class TestGetAbiAndAddress:
    async def test_get_abi_success(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            scripted_rp,
            "uncached_get_abi_by_name",
            AsyncMock(return_value='[{"name": "foo"}]'),
            raising=False,
        )
        cog = Random(make_bot())
        interaction = make_interaction()
        await cog.get_abi_of_contract.callback(
            cog, interaction, contract="rocketDepositPool"
        )
        # Sent as a file attachment.
        assert "file" in interaction.followup.send.call_args.kwargs

    async def test_get_abi_failure_reports_exception(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            scripted_rp,
            "uncached_get_abi_by_name",
            AsyncMock(side_effect=RuntimeError("no abi")),
            raising=False,
        )
        cog = Random(make_bot())
        interaction = make_interaction()
        await cog.get_abi_of_contract.callback(cog, interaction, contract="bogus")
        content = interaction.followup.send.call_args.kwargs["content"]
        assert "Exception" in content

    async def test_get_address_from_manual_config(
        self, scripted_rp: ScriptedRocketPool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_el(target: str, *_: Any, **__: Any) -> str:
            return f"[{target}](el)"

        monkeypatch.setattr(random_module, "el_explorer_url", fake_el)
        cog = Random(make_bot())
        interaction = make_interaction()
        # rocketStorage is in the baseline cfg's manual_addresses.
        await cog.get_address_of_contract.callback(
            cog, interaction, contract="rocketStorage"
        )
        interaction.followup.send.assert_awaited()


class TestBurnReason:
    async def test_renders_burn_leaderboard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data = {
            "feesBurned": {
                "feesBurned5m": 1 * ETH,
                "feesBurned5mUsd": 3000,
                "feesBurned1h": 12 * ETH,
                "feesBurned1hUsd": 36000,
                "feesBurned24h": 288 * ETH,
                "feesBurned24hUsd": 864000,
            },
            "leaderboards": {
                "leaderboard5m": [
                    {
                        "name": "Uniswap",
                        "address": "0xabc",
                        "fees": ETH // 2,
                        "category": "dex",
                    },
                    {"name": "", "address": "0xdef", "fees": ETH // 4},
                    {"name": "Contract Deployment", "fees": ETH // 8},
                ]
            },
            "latestBlockFees": [{"baseFeePerGas": 20 * 10**9}],
        }
        _patch_http(monkeypatch, data)
        cog = Random(make_bot())
        interaction = make_interaction()
        await cog.burn_reason.callback(cog, interaction)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.description is not None
        assert "ETH Burned" in embed.description
        assert "Uniswap" in embed.description


class TestAsianRestaurantName:
    async def test_returns_api_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_http(monkeypatch, {"name": "Golden Dragon"})
        cog = Random(make_bot())
        interaction = make_interaction()
        await cog.asian_restaurant_name.callback(cog, interaction)
        assert interaction.followup.send.call_args.args[0] == "Golden Dragon"


class TestSmoothie:
    async def test_no_validators_returns_message(
        self,
        scripted_rp: ScriptedRocketPool,
        mongo_db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scripted_rp.set_address("rocketSmoothingPool", addr("0x" + "33" * 20))
        monkeypatch.setattr(
            random_module.w3,
            "eth",
            AsyncMock(get_balance=AsyncMock(return_value=10 * ETH)),
            raising=False,
        )
        scripted_rp.set_call("rocketRewardsPool.getClaimIntervalTimeStart", 0)

        cog = Random(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.smoothie.callback(cog, interaction)
        # Empty DB → "No validators found." message.
        assert interaction.followup.send.call_args.args[0] == "No validators found."

    async def test_renders_smoothie_stats(
        self,
        scripted_rp: ScriptedRocketPool,
        mongo_db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_el(target: str, *_: Any, **__: Any) -> str:
            return f"[{target}](el)"

        monkeypatch.setattr(random_module, "el_explorer_url", fake_el)
        monkeypatch.setattr(
            random_module.w3,
            "eth",
            AsyncMock(get_balance=AsyncMock(return_value=42 * ETH)),
            raising=False,
        )
        scripted_rp.set_address("rocketSmoothingPool", addr("0x" + "33" * 20))
        scripted_rp.set_call("rocketRewardsPool.getClaimIntervalTimeStart", 0)

        # Node A is in the smoothing pool with 2 minipools; node B is not, 1.
        await mongo_db.minipools.insert_many(
            [
                {"node_operator": "0xA", "beacon": {"status": "active_ongoing"}},
                {"node_operator": "0xA", "beacon": {"status": "active_ongoing"}},
                {"node_operator": "0xB", "beacon": {"status": "active_ongoing"}},
            ]
        )
        await mongo_db.node_operators.insert_many(
            [
                {"address": "0xA", "smoothing_pool_registration": True},
                {"address": "0xB", "smoothing_pool_registration": False},
            ]
        )

        cog = Random(make_bot(db=mongo_db))
        interaction = make_interaction()
        await cog.smoothie.callback(cog, interaction)
        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.title == "Smoothing Pool"
        assert embed.description is not None
        assert "smoothing pool" in embed.description
        assert "42.00" in embed.description  # balance


class TestSeaCreatures:
    async def test_lists_all_when_no_address(self) -> None:
        cog = Random(make_bot())
        interaction = make_interaction()
        await cog.sea_creatures.callback(cog, interaction)
        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.title == "Possible Sea Creatures"
        assert embed.description

    async def test_invalid_address_reports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # to_checksum_address raises → "Invalid address".
        def bad_checksum(_a: str) -> str:
            raise ValueError("bad address")

        monkeypatch.setattr(
            random_module.w3, "to_checksum_address", bad_checksum, raising=False
        )
        cog = Random(make_bot())
        interaction = make_interaction()
        await cog.sea_creatures.callback(cog, interaction, address="garbage")
        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.description == "Invalid address"

    @pytest.fixture
    def _stub_holdings(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        monkeypatch.setattr(
            random_module.w3, "to_checksum_address", lambda a: a, raising=False
        )

        async def fake_el(target: str, *_: Any, **kw: Any) -> str:
            return f"[{kw.get('prefix', '')}{target}](el)"

        monkeypatch.setattr(random_module, "el_explorer_url", fake_el)
        yield

    async def test_known_creature_for_whale(
        self, monkeypatch: pytest.MonkeyPatch, _stub_holdings: None
    ) -> None:
        monkeypatch.setattr(
            random_module,
            "get_sea_creature_for_address",
            AsyncMock(return_value="🐳"),
        )
        monkeypatch.setattr(
            random_module, "get_holding_for_address", AsyncMock(return_value=5000.0)
        )
        cog = Random(make_bot())
        interaction = make_interaction()
        await cog.sea_creatures.callback(cog, interaction, address="0x" + "ab" * 20)
        embed = interaction.followup.send.call_args.kwargs["embed"]
        field_names = [f.name for f in embed.fields]
        assert "Actual Holding" in field_names

    async def test_no_creature_for_small_holder(
        self, monkeypatch: pytest.MonkeyPatch, _stub_holdings: None
    ) -> None:
        monkeypatch.setattr(
            random_module,
            "get_sea_creature_for_address",
            AsyncMock(return_value=""),
        )
        cog = Random(make_bot())
        interaction = make_interaction()
        await cog.sea_creatures.callback(cog, interaction, address="0x" + "cd" * 20)
        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.description is not None
        assert "No sea creature" in embed.description
