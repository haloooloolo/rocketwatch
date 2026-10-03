"""End-to-end tests for log event processing: chain logs in, Discord events out.

Logs are ABI-encoded for minimal real web3 contracts and served through
``EventLogScript``, so filters, decoding, aggregation, global-event enrichment
and event wrapping all run for real.
"""

from collections import Counter
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_abi.abi import encode
from eth_typing import BlockNumber, ChecksumAddress
from web3 import Web3
from web3.contract import Contract
from web3.types import LogReceipt

from rocketwatch.plugins.log_events import event_definitions as defs
from rocketwatch.plugins.log_events.log_events import LogEvents
from rocketwatch.utils import shared_w3
from rocketwatch.utils.event import Event
from rocketwatch.utils.rocketpool import NoAddressFound
from tests.lib.discord_harness import make_bot
from tests.lib.event_log_script import EventLogScript, make_log
from tests.lib.explorer import stub_explorer_links
from tests.lib.scripted_rocketpool import ScriptedRocketPool

ETH = 10**18


def _addr(n: int) -> ChecksumAddress:
    return Web3.to_checksum_address(f"0x{n:040x}")


STAKING, DEPOSIT_POOL, RETH, UPGRADE, MEGAPOOL_DELEGATE = (
    _addr(0xA1),
    _addr(0xA2),
    _addr(0xA3),
    _addr(0xA4),
    _addr(0xA5),
)
MINIPOOL_DELEGATE = _addr(0xA6)
NEW_STAKING = _addr(0xB1)
NODE, MINIPOOL, MEGAPOOL, STRANGER = _addr(0x11), _addr(0x12), _addr(0x13), _addr(0x99)


def _event(name: str, *inputs: tuple[str, str, bool]) -> dict[str, Any]:
    return {
        "type": "event",
        "name": name,
        "anonymous": False,
        "inputs": [{"name": n, "type": t, "indexed": i} for n, t, i in inputs],
    }


ABIS: dict[str, list[dict[str, Any]]] = {
    "rocketNodeStaking": [
        _event(
            "RPLStaked",
            ("from", "address", True),
            ("caller", "address", True),
            ("amount", "uint256", False),
            ("time", "uint256", False),
        ),
        _event(
            "RPLWithdrawn",
            ("to", "address", True),
            ("amount", "uint256", False),
            ("time", "uint256", False),
        ),
    ],
    "rocketDepositPool": [
        _event(
            "DepositAssigned",
            ("minipool", "address", True),
            ("amount", "uint256", False),
            ("time", "uint256", False),
        ),
    ],
    "rocketTokenRETH": [
        _event(
            "Transfer",
            ("from", "address", True),
            ("to", "address", True),
            ("value", "uint256", False),
        ),
        _event(
            "TokensBurned",
            ("from", "address", True),
            ("amount", "uint256", False),
            ("ethAmount", "uint256", False),
            ("time", "uint256", False),
        ),
    ],
    "rocketDAONodeTrustedUpgrade": [
        _event(
            "ContractUpgraded",
            ("name", "bytes32", True),
            ("oldAddress", "address", True),
            ("newAddress", "address", True),
            ("time", "uint256", False),
        ),
    ],
    "rocketMinipoolDelegate": [
        _event(
            "StatusUpdated",
            ("status", "uint8", True),
            ("time", "uint256", False),
        ),
    ],
    "rocketMegapoolDelegate": [
        _event(
            "MegapoolValidatorExiting",
            ("validatorId", "uint32", True),
            ("time", "uint256", False),
        ),
    ],
}


class Chain:
    """Scripted contracts, logs and receipts behind ``rp`` and ``w3``."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
    ) -> None:
        self.rp = scripted_rp
        self.logs = EventLogScript()
        self.receipts: dict[bytes, dict[str, Any]] = {}
        self.contracts: dict[str, Contract] = {}
        self._tx_count = 0
        for name, address in [
            ("rocketNodeStaking", STAKING),
            ("rocketDepositPool", DEPOSIT_POOL),
            ("rocketTokenRETH", RETH),
            ("rocketDAONodeTrustedUpgrade", UPGRADE),
            ("rocketMegapoolDelegate", MEGAPOOL_DELEGATE),
            ("rocketMinipoolDelegate", MINIPOOL_DELEGATE),
        ]:
            self.deploy(name, address)

        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", ETH // 100)
        scripted_rp.set_call("rocketMinipool.getNodeAddress", NODE)
        scripted_rp.set_call("rocketMegapoolDelegate.getNodeAddress", NODE)
        scripted_rp.set_call("rocketMinipoolManager.getMinipoolPubkey", b"")
        scripted_rp.set_call("rocketMinipoolDelegate.getNodeAddress", NODE)
        scripted_rp.mark_megapool(MEGAPOOL)
        scripted_rp.mark_minipool(MINIPOOL)
        for name, method in [
            ("get_contract_by_name", self._contract_by_name),
            ("get_contract_by_address", self._contract_by_address),
        ]:
            monkeypatch.setattr(scripted_rp, name, method, raising=False)

        eth = MagicMock()
        eth.get_logs = self.logs.get_logs
        eth.get_transaction_receipt = AsyncMock(
            side_effect=lambda tx: self.receipts[bytes(tx)]
        )
        w3_stub = MagicMock(eth=eth)
        w3_stub.keccak = Web3.keccak
        w3_stub.to_hex = Web3.to_hex
        w3_stub.to_checksum_address = Web3.to_checksum_address
        monkeypatch.setattr(shared_w3.w3, "_instance", w3_stub)

    def deploy(self, name: str, address: ChecksumAddress) -> None:
        self.contracts[name] = Web3().eth.contract(address=address, abi=ABIS[name])
        self.rp.set_address(name, address)

    async def _contract_by_name(self, name: str, mainnet: bool = False) -> Any:
        if name == "casperDeposit":
            deposit_event = SimpleNamespace(process_receipt=lambda _receipt: [])
            return SimpleNamespace(
                events=SimpleNamespace(DepositEvent=lambda: deposit_event)
            )
        if name not in self.contracts:
            raise NoAddressFound(name)
        return self.contracts[name]

    async def _contract_by_address(self, address: ChecksumAddress) -> Any:
        name = self.rp.get_name_by_address(address)
        return self.contracts[name] if name else None

    def tx(self, *, to: ChecksumAddress = STAKING) -> bytes:
        self._tx_count += 1
        tx_hash = bytes([self._tx_count]) * 32
        self.receipts[tx_hash] = {"to": to, "from": NODE}
        return tx_hash

    def emit(
        self,
        contract: str,
        event: str,
        args: dict[str, Any],
        *,
        block: int,
        tx: bytes,
        log_index: int = 0,
        address: ChecksumAddress | None = None,
        removed: bool = False,
    ) -> LogReceipt:
        inputs = self.contracts[contract].events[event].abi["inputs"]
        signature = f"{event}({','.join(i['type'] for i in inputs)})"
        indexed = [i for i in inputs if i["indexed"]]
        plain = [i for i in inputs if not i["indexed"]]
        log = make_log(
            address=address or self.contracts[contract].address,
            topics=[
                Web3.keccak(text=signature),
                *(encode([i["type"]], [args[i["name"]]]) for i in indexed),
            ],
            data=encode([i["type"] for i in plain], [args[i["name"]] for i in plain]),
            block_number=block,
            transaction_hash=tx,
            transaction_index=tx[0],
            log_index=log_index,
            removed=removed,
        )
        self.logs.add(log)
        return log


@pytest.fixture
def chain(
    monkeypatch: pytest.MonkeyPatch,
    scripted_rp: ScriptedRocketPool,
    testnet_cfg: None,
) -> Chain:
    stub_explorer_links(monkeypatch, defs)
    return Chain(monkeypatch, scripted_rp)


@pytest.fixture
async def cog(chain: Chain) -> LogEvents:
    cog = LogEvents(make_bot())
    await cog.async_init()
    return cog


async def _scan(cog: LogEvents, to_block: int = 20) -> list[Event]:
    return await cog.get_past_events(BlockNumber(1), BlockNumber(to_block))


def _withdrawal(chain: Chain, amount: int, **kwargs: Any) -> LogReceipt:
    return chain.emit(
        "rocketNodeStaking",
        "RPLWithdrawn",
        {"to": NODE, "amount": amount * ETH, "time": 0},
        **kwargs,
    )


class TestDirectEvents:
    async def test_withdrawal_becomes_an_event(
        self, cog: LogEvents, chain: Chain
    ) -> None:
        tx = chain.tx()
        _withdrawal(chain, 2_000, block=5, tx=tx, log_index=3)

        [event] = await _scan(cog)

        assert event.topic == "events"
        assert event.event_name == "rpl_withdraw_event"
        assert (event.block_number, event.transaction_index, event.event_index) == (
            5,
            tx[0],
            3,
        )
        assert tx.hex() in event.unique_id
        assert event.embed.description is not None
        assert "withdrew **2,000 RPL** (worth 20 ETH)" in event.embed.description

    async def test_small_withdrawal_is_not_posted(
        self, cog: LogEvents, chain: Chain
    ) -> None:
        # 100 RPL is worth 1 ETH, below the 16 ETH threshold
        _withdrawal(chain, 100, block=5, tx=chain.tx())

        assert await _scan(cog) == []

    async def test_event_registered_by_full_signature(
        self, cog: LogEvents, chain: Chain
    ) -> None:
        chain.emit(
            "rocketNodeStaking",
            "RPLStaked",
            {"from": NODE, "caller": NODE, "amount": 5_000 * ETH, "time": 0},
            block=5,
            tx=chain.tx(),
        )

        [event] = await _scan(cog)

        assert event.event_name == "rpl_stake_event"

    async def test_removed_log_is_ignored(self, cog: LogEvents, chain: Chain) -> None:
        _withdrawal(chain, 2_000, block=5, tx=chain.tx(), removed=True)

        assert await _scan(cog) == []

    async def test_repeated_events_in_one_tx_get_distinct_ids(
        self, cog: LogEvents, chain: Chain
    ) -> None:
        tx = chain.tx()
        _withdrawal(chain, 2_000, block=5, tx=tx, log_index=0)
        _withdrawal(chain, 3_000, block=5, tx=tx, log_index=1)
        _withdrawal(chain, 3_000, block=5, tx=tx, log_index=2)

        events = await _scan(cog)

        assert len({e.unique_id for e in events}) == 3

    async def test_events_are_ordered_by_position_on_chain(
        self, cog: LogEvents, chain: Chain
    ) -> None:
        late, early = chain.tx(), chain.tx()
        _withdrawal(chain, 3_000, block=6, tx=late)
        _withdrawal(chain, 2_000, block=5, tx=early, log_index=4)
        _withdrawal(chain, 4_000, block=5, tx=early, log_index=1)

        events = await _scan(cog)

        scores = [e.get_score() for e in events]
        assert scores == sorted(scores)


def _assign(chain: Chain, tx: bytes, log_index: int) -> None:
    chain.emit(
        "rocketDepositPool",
        "DepositAssigned",
        {"minipool": MINIPOOL, "amount": 31 * ETH, "time": 0},
        block=5,
        tx=tx,
        log_index=log_index,
    )


class TestAggregation:
    async def test_assignments_in_one_tx_are_summarised(
        self, cog: LogEvents, chain: Chain
    ) -> None:
        tx = chain.tx(to=DEPOSIT_POOL)
        for i in range(3):
            _assign(chain, tx, i)

        [event] = await _scan(cog)

        assert event.event_name == "pool_deposit_assigned_event"
        assert event.embed.description is not None
        assert "3 minipools have been matched" in event.embed.description

    async def test_single_assignment_names_the_minipool(
        self, cog: LogEvents, chain: Chain
    ) -> None:
        _assign(chain, chain.tx(to=DEPOSIT_POOL), 0)

        [event] = await _scan(cog)

        assert event.event_name == "pool_deposit_assigned_single_event"
        assert event.embed.description is not None
        assert MINIPOOL in event.embed.description
        assert NODE in event.embed.description

    @staticmethod
    def _transfer(chain: Chain, tx: bytes, log_index: int = 0) -> None:
        chain.emit(
            "rocketTokenRETH",
            "Transfer",
            {"from": NODE, "to": STRANGER, "value": 1_500 * ETH},
            block=5,
            tx=tx,
            log_index=log_index,
        )

    async def test_large_transfer_is_posted(self, cog: LogEvents, chain: Chain) -> None:
        self._transfer(chain, chain.tx(to=RETH))

        [event] = await _scan(cog)

        assert event.event_name == "reth_transfer_event"

    async def test_burn_supersedes_its_transfer(
        self, cog: LogEvents, chain: Chain
    ) -> None:
        tx = chain.tx(to=RETH)
        self._transfer(chain, tx, 0)
        chain.emit(
            "rocketTokenRETH",
            "TokensBurned",
            {"from": NODE, "amount": 1_500 * ETH, "ethAmount": 1_700 * ETH, "time": 0},
            block=5,
            tx=tx,
            log_index=1,
        )

        events = await _scan(cog)

        assert [e.event_name for e in events] == ["reth_burn_event"]


class TestGlobalEvents:
    async def test_megapool_event_is_attributed_to_its_node(
        self, cog: LogEvents, chain: Chain
    ) -> None:
        chain.emit(
            "rocketMegapoolDelegate",
            "MegapoolValidatorExiting",
            {"validatorId": 7, "time": 0},
            block=5,
            tx=chain.tx(to=MEGAPOOL),
            address=MEGAPOOL,
        )

        [event] = await _scan(cog)

        assert event.event_name == "megapool_validator_exiting_event"
        assert event.embed.description is not None
        assert "Validator 7 of node" in event.embed.description
        assert NODE in event.embed.description

    async def test_lookalike_event_from_unknown_contract_is_ignored(
        self, cog: LogEvents, chain: Chain
    ) -> None:
        # global filters match by topic only, so anyone can emit a lookalike
        chain.emit(
            "rocketMegapoolDelegate",
            "MegapoolValidatorExiting",
            {"validatorId": 7, "time": 0},
            block=5,
            tx=chain.tx(to=STRANGER),
            address=STRANGER,
        )

        assert await _scan(cog) == []

    @staticmethod
    def _status_update(chain: Chain, status: int) -> None:
        chain.emit(
            "rocketMinipoolDelegate",
            "StatusUpdated",
            {"status": status, "time": 0},
            block=5,
            tx=chain.tx(to=MINIPOOL),
            address=MINIPOOL,
        )

    async def test_dissolve_status_is_posted_as_a_dissolve(
        self, cog: LogEvents, chain: Chain
    ) -> None:
        self._status_update(chain, 4)

        [event] = await _scan(cog)

        assert event.event_name == "minipool_dissolve_event"
        assert event.embed.title == ":rotating_light: Minipool Dissolved"

    async def test_other_status_changes_are_not_posted(
        self, cog: LogEvents, chain: Chain
    ) -> None:
        self._status_update(chain, 2)

        assert await _scan(cog) == []


class TestContractUpgrade:
    async def test_events_after_upgrade_come_from_the_new_contract_once(
        self,
        cog: LogEvents,
        chain: Chain,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chain.emit(
            "rocketDAONodeTrustedUpgrade",
            "ContractUpgraded",
            {
                "name": Web3.keccak(text="rocketNodeStaking"),
                "oldAddress": STAKING,
                "newAddress": NEW_STAKING,
                "time": 0,
            },
            block=10,
            tx=chain.tx(to=UPGRADE),
        )
        _withdrawal(chain, 2_000, block=12, tx=chain.tx(), address=NEW_STAKING)
        chain.emit(
            "rocketTokenRETH",
            "Transfer",
            {"from": NODE, "to": STRANGER, "value": 1_500 * ETH},
            block=12,
            tx=chain.tx(to=RETH),
        )

        async def flush() -> None:
            # the reload sees the upgraded address from rocketStorage
            chain.deploy("rocketNodeStaking", NEW_STAKING)

        monkeypatch.setattr(chain.rp, "flush", flush)

        events = await _scan(cog)

        assert Counter(e.event_name for e in events) == {
            "odao_contract_upgraded_event": 1,
            "rpl_withdraw_event": 1,
            "reth_transfer_event": 1,
        }
