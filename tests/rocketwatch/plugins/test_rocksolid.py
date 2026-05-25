from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from rocketwatch.plugins.rocksolid import rocksolid as rocksolid_module
from rocketwatch.plugins.rocksolid.rocksolid import RockSolid
from rocketwatch.utils import shared_w3 as sw
from tests.lib.discord_harness import make_bot, make_interaction
from tests.lib.scripted_rocketpool import ScriptedRocketPool, addr

ETH = 10**18
DEPLOY_BLOCK = 23_237_366


async def _run(cmd: Any, cog: Any, interaction: Any) -> None:
    await cmd.callback(cog, interaction)


def _aiter(items: list[Any]) -> AsyncIterator[Any]:
    async def gen() -> AsyncIterator[Any]:
        for item in items:
            yield item

    return gen()


class _Txn:
    async def __aenter__(self) -> "_Txn":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Session:
    async def start_transaction(self) -> _Txn:
        return _Txn()


class _SessionCM:
    async def __aenter__(self) -> _Session:
        return _Session()

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _fake_db(*, last_checked: dict[str, Any] | None, stored: list[Any]) -> MagicMock:
    db = MagicMock()
    db.last_checked_block.find_one = AsyncMock(return_value=last_checked)
    db.last_checked_block.replace_one = AsyncMock()
    db.rocksolid.find = MagicMock(return_value=_aiter(stored))
    db.rocksolid.bulk_write = AsyncMock()
    db.client.start_session = lambda: _SessionCM()
    return db


@pytest.fixture
def _stub_externals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def block_number() -> int:
        return DEPLOY_BLOCK + 1_000_000

    async def block_to_ts(_b: int) -> int:
        return 1_700_000_000

    monkeypatch.setattr(
        sw.w3,
        "eth",
        AsyncMock(get_block_number=block_number),
        raising=False,
    )
    monkeypatch.setattr(rocksolid_module, "block_to_ts", block_to_ts)

    async def fake_el(target: str, *_: Any, **kw: Any) -> str:
        return f"[{kw.get('name', target)}](el)"

    monkeypatch.setattr(rocksolid_module, "el_explorer_url", fake_el)
    yield


def _seed_rates(scripted_rp: ScriptedRocketPool) -> None:
    # convertToAssets / getEthValue compose into the eth-per-share rate.
    # Return a value that grows with block so APY is positive.
    scripted_rp.set_call("RockSolidVault.convertToAssets", lambda shares, **_: shares)
    scripted_rp.set_call(
        "rocketTokenRETH.getEthValue",
        lambda value, **_: int(value * 1.05),
    )
    scripted_rp.set_call("RockSolidVault.totalAssets", 1000 * ETH)
    scripted_rp.set_call("RockSolidVault.totalSupply", 950 * ETH)
    scripted_rp.set_address("rocketTokenRETH", addr("0x" + "11" * 20))
    scripted_rp.set_address("RockSolidVault", addr("0x" + "22" * 20))


class TestRockSolidCommand:
    async def test_renders_full_embed(
        self,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
        _stub_externals: None,
    ) -> None:
        _seed_rates(scripted_rp)
        # ts_to_block returns a block well past deployment so APY is computed.
        monkeypatch.setattr(
            rocksolid_module,
            "ts_to_block",
            AsyncMock(return_value=DEPLOY_BLOCK + 500_000),
        )

        cog = RockSolid(make_bot())
        monkeypatch.setattr(
            cog,
            "_fetch_asset_updates",
            AsyncMock(
                return_value=[
                    (1_699_000_000, 100.0),
                    (1_699_500_000, 150.0),
                    (1_700_000_000, 200.0),
                ]
            ),
        )

        interaction = make_interaction()
        await _run(cog.rocksolid, cog, interaction)

        kwargs = interaction.followup.send.call_args.kwargs
        embed = kwargs["embed"]
        assert embed.title is not None and "RockSolid" in embed.title
        field_names = [f.name for f in embed.fields]
        assert "7d APY" in field_names
        assert "TVL" in field_names
        assert "Supply" in field_names
        assert kwargs["file"].filename == "rocksolid-tvl.png"

    async def test_apy_dash_when_reference_predates_deployment(
        self,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
        _stub_externals: None,
    ) -> None:
        _seed_rates(scripted_rp)
        # ts_to_block returns a block *before* deployment → every APY is None ("-").
        monkeypatch.setattr(
            rocksolid_module,
            "ts_to_block",
            AsyncMock(return_value=DEPLOY_BLOCK - 1),
        )

        cog = RockSolid(make_bot())
        monkeypatch.setattr(
            cog,
            "_fetch_asset_updates",
            AsyncMock(return_value=[(1_700_000_000, 200.0)]),
        )

        interaction = make_interaction()
        await _run(cog.rocksolid, cog, interaction)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        apy_fields = {f.name: f.value for f in embed.fields if "APY" in f.name}
        assert all(v == "-" for v in apy_fields.values())


class TestFetchAssetUpdates:
    async def test_ingests_new_logs_and_persists(
        self,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
        _stub_externals: None,
    ) -> None:
        monkeypatch.setattr(
            rocksolid_module,
            "get_logs",
            AsyncMock(
                return_value=[{"blockNumber": 100, "args": {"totalAssets": 200 * ETH}}]
            ),
        )
        db = _fake_db(last_checked=None, stored=[{"time": 1, "assets": 50.0}])
        cog = RockSolid(make_bot(db=db))

        updates = await cog._fetch_asset_updates()

        # both the stored doc and the freshly-decoded log are returned
        assert (1, 50.0) in updates
        assert (1_700_000_000, 200.0) in updates
        # new logs are written and the checkpoint advanced
        db.rocksolid.bulk_write.assert_awaited_once()
        db.last_checked_block.replace_one.assert_awaited_once()

    async def test_no_new_logs_skips_bulk_write(
        self,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
        _stub_externals: None,
    ) -> None:
        monkeypatch.setattr(rocksolid_module, "get_logs", AsyncMock(return_value=[]))
        db = _fake_db(last_checked={"block": DEPLOY_BLOCK + 5}, stored=[])
        cog = RockSolid(make_bot(db=db))

        updates = await cog._fetch_asset_updates()

        assert updates == []
        db.rocksolid.bulk_write.assert_not_awaited()
        # the checkpoint is still advanced even with nothing new
        db.last_checked_block.replace_one.assert_awaited_once()


class TestSetup:
    async def test_registers_cog(self) -> None:
        bot = make_bot()
        bot.add_cog = AsyncMock()
        await rocksolid_module.setup(bot)
        bot.add_cog.assert_awaited_once()
