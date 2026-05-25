import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.abc import Messageable
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.user_distribute import user_distribute as ud_module
from rocketwatch.plugins.user_distribute.user_distribute import UserDistribute
from rocketwatch.utils import shared_w3 as sw
from rocketwatch.utils.config import cfg
from tests.lib.cfg import make_cfg
from tests.lib.discord_harness import make_bot, make_interaction
from tests.lib.scripted_rocketpool import ScriptedRocketPool

NOW = 1_700_000_000
WINDOW_START = 100
WINDOW_LENGTH = 200  # window: [start, start+length) = [100, 300)


class _Eth:
    """Minimal AsyncEth stand-in exposing an awaitable gas_price property."""

    @property
    def gas_price(self) -> Any:
        async def _value() -> int:
            return 10**9

        return _value()


async def _run(cmd: Any, cog: Any, interaction: Any) -> None:
    await cmd.callback(cog, interaction)


def _make_cog(bot: Any) -> UserDistribute:
    # __init__ starts a tasks.loop; bypass it.
    cog = UserDistribute.__new__(UserDistribute)
    cog.bot = bot
    return cog


@pytest.fixture
def _stub_env(
    monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
) -> Iterator[dict[str, int]]:
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(sw.w3, "to_checksum_address", lambda a: a, raising=False)
    scripted_rp.set_call(
        "rocketDAOProtocolSettingsMinipool.getUserDistributeWindowStart", WINDOW_START
    )
    scripted_rp.set_call(
        "rocketDAOProtocolSettingsMinipool.getUserDistributeWindowLength", WINDOW_LENGTH
    )
    # Per-address user_distribute_time, served via get_storage_at.
    storage: dict[str, int] = {}

    async def get_storage_at(address: str, _slot: int) -> bytes:
        return storage.get(address, 0).to_bytes(32, "big")

    monkeypatch.setattr(
        sw.w3, "eth", AsyncMock(get_storage_at=get_storage_at), raising=False
    )
    yield storage


def _minipool(address: str) -> dict[str, Any]:
    return {
        "address": address,
        "user_distributed": False,
        "status": "staking",
        "execution_balance": 10,
        "beacon": {"withdrawable_epoch": 1},  # below threshold for any head slot
    }


class TestFetchMinipools:
    async def test_classifies_pending_distributable_eligible(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_bacon: Any,
        _stub_env: dict[str, int],
    ) -> None:
        # head slot high enough that threshold_epoch > 1 (minipools qualify).
        scripted_bacon.set_block_header("head", {"slot": str(6000 * 32)})

        await mongo_db.minipools.insert_many(
            [_minipool("0xPEND"), _minipool("0xDIST"), _minipool("0xELIG")]
        )
        # elapsed = NOW - udt. pending: elapsed<100; distributable: 100<=e<300;
        # eligible: e>=300.
        _stub_env["0xPEND"] = NOW - 50
        _stub_env["0xDIST"] = NOW - 200
        _stub_env["0xELIG"] = NOW - 400
        # The eligible double-check: not yet distributed/finalised → stays eligible.
        scripted_rp.set_call("rocketMinipool.getUserDistributed", False)
        scripted_rp.set_call("rocketMinipool.getFinalised", False)

        cog = _make_cog(make_bot(db=mongo_db))
        eligible, pending, distributable = await cog._fetch_minipools()

        assert [m["address"] for m in pending] == ["0xPEND"]
        assert [m["address"] for m in distributable] == ["0xDIST"]
        assert [m["address"] for m in eligible] == ["0xELIG"]
        # Window fields are stamped for sorting/display.
        assert pending[0]["ud_window_open"] == (NOW - 50) + WINDOW_START
        assert distributable[0]["ud_window_close"] == (NOW - 200) + (
            WINDOW_START + WINDOW_LENGTH
        )

    async def test_eligible_skipped_when_already_distributed(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_bacon: Any,
        _stub_env: dict[str, int],
    ) -> None:
        scripted_bacon.set_block_header("head", {"slot": str(6000 * 32)})
        await mongo_db.minipools.insert_one(_minipool("0xDONE"))
        _stub_env["0xDONE"] = NOW - 400  # past the window → eligible branch
        # DB lagged: chain says it's already distributed → skip.
        scripted_rp.set_call("rocketMinipool.getUserDistributed", True)
        scripted_rp.set_call("rocketMinipool.getFinalised", False)

        cog = _make_cog(make_bot(db=mongo_db))
        eligible, pending, distributable = await cog._fetch_minipools()
        assert eligible == []
        assert pending == []
        assert distributable == []

    async def test_eligible_skipped_when_finalised(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_bacon: Any,
        _stub_env: dict[str, int],
    ) -> None:
        scripted_bacon.set_block_header("head", {"slot": str(6000 * 32)})
        await mongo_db.minipools.insert_one(_minipool("0xFIN"))
        _stub_env["0xFIN"] = NOW - 400
        scripted_rp.set_call("rocketMinipool.getUserDistributed", False)
        scripted_rp.set_call("rocketMinipool.getFinalised", True)

        cog = _make_cog(make_bot(db=mongo_db))
        eligible, _, _ = await cog._fetch_minipools()
        assert eligible == []


class TestUserDistributeStatusCommand:
    async def test_reports_all_three_categories(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_bacon: Any,
        _stub_env: dict[str, int],
    ) -> None:
        scripted_bacon.set_block_header("head", {"slot": str(6000 * 32)})
        await mongo_db.minipools.insert_many(
            [_minipool("0xPEND"), _minipool("0xDIST"), _minipool("0xELIG")]
        )
        _stub_env["0xPEND"] = NOW - 50
        _stub_env["0xDIST"] = NOW - 200
        _stub_env["0xELIG"] = NOW - 400
        scripted_rp.set_call("rocketMinipool.getUserDistributed", False)
        scripted_rp.set_call("rocketMinipool.getFinalised", False)

        cog = _make_cog(make_bot(db=mongo_db))
        interaction = make_interaction()
        await _run(cog.user_distribute_status, cog, interaction)

        kwargs = interaction.followup.send.call_args.kwargs
        embed = kwargs["embed"]
        assert embed.title == "User Distribute Status"
        fields = {f.name: f.value for f in embed.fields}
        assert "1" in fields["Eligible"]
        assert "window opens" in fields["Pending"]
        assert "window closes" in fields["Distributable"]
        # eligible or distributable present → an InstructionsView is attached.
        assert "view" in kwargs

    async def test_empty_state_sends_plain_embed(
        self,
        mongo_db: AsyncDatabase[dict[str, Any]],
        scripted_rp: ScriptedRocketPool,
        scripted_bacon: Any,
        _stub_env: dict[str, int],
    ) -> None:
        scripted_bacon.set_block_header("head", {"slot": str(6000 * 32)})
        # No minipools at all.
        cog = _make_cog(make_bot(db=mongo_db))
        interaction = make_interaction()
        await _run(cog.user_distribute_status, cog, interaction)

        kwargs = interaction.followup.send.call_args.kwargs
        # No eligible/distributable → no view attached.
        assert "view" not in kwargs
        fields = {f.name: f.value for f in kwargs["embed"].fields}
        assert fields["Eligible"] == "**0** minipools"
        assert fields["Pending"] == "**0** minipools"
        assert fields["Distributable"] == "**0** minipools"


class TestInstructionsView:
    async def test_builds_embed_and_input_file(
        self, monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
    ) -> None:
        mp_contract = MagicMock()
        mp_contract.encode_abi = MagicMock(return_value="0xabcdef")
        agg = MagicMock()
        agg.estimate_gas = AsyncMock(return_value=210000)
        multicall = MagicMock()
        multicall.address = "0xMULTI"
        multicall.functions.aggregate3 = MagicMock(return_value=agg)
        monkeypatch.setattr(
            scripted_rp, "assemble_contract", AsyncMock(return_value=mp_contract)
        )
        monkeypatch.setattr(
            scripted_rp, "get_contract_by_name", AsyncMock(return_value=multicall)
        )
        monkeypatch.setattr(sw.w3, "eth", _Eth(), raising=False)

        view = ud_module.InstructionsView(
            eligible=[{"address": "0xA"}],
            distributable=[{"address": "0xB"}],
            instruction_timeout=1800,
        )
        interaction = make_interaction()
        interaction.response.send_message = AsyncMock()

        await view.instructions.callback(interaction)

        kwargs = interaction.response.send_message.call_args.kwargs
        embed = kwargs["embed"]
        assert embed.title == "Distribution Instructions"
        assert embed.description is not None
        assert "distribute the balance" in embed.description
        assert "begin the user distribution" in embed.description
        assert kwargs["file"].filename == "input_data.txt"


class TestTaskLoop:
    def _channel_cfg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = make_cfg()
        c.discord.channels["user_distribute"] = 555
        monkeypatch.setattr(cfg, "_instance", c)

    async def test_no_channel_configured_is_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Baseline cfg has no user_distribute channel → returns before fetching.
        cog = _make_cog(make_bot())
        fetch = AsyncMock()
        monkeypatch.setattr(cog, "_fetch_minipools", fetch)
        await cog.task.coro(cog)
        fetch.assert_not_awaited()

    async def test_sends_window_open_notification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._channel_cfg(monkeypatch)
        channel = MagicMock(spec=Messageable)
        channel.send = AsyncMock()
        bot = make_bot()
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        cog = _make_cog(bot)
        dist = [{"address": "0xB", "ud_window_close": NOW + 1000}]
        monkeypatch.setattr(
            cog, "_fetch_minipools", AsyncMock(return_value=([], [], dist))
        )

        await cog.task.coro(cog)

        channel.send.assert_awaited_once()
        assert "view" in channel.send.call_args.kwargs

    async def test_no_distributable_is_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._channel_cfg(monkeypatch)
        channel = MagicMock(spec=Messageable)
        channel.send = AsyncMock()
        bot = make_bot()
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        cog = _make_cog(bot)
        monkeypatch.setattr(
            cog, "_fetch_minipools", AsyncMock(return_value=([], [], []))
        )

        await cog.task.coro(cog)

        channel.send.assert_not_awaited()

    async def test_on_task_error_reports(self) -> None:
        bot = make_bot()
        cog = _make_cog(bot)
        await cog.on_task_error(RuntimeError("boom"))
        bot.report_error.assert_awaited_once()
