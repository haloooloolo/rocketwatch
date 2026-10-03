from dataclasses import dataclass, field
from typing import Any

import pytest
from eth_typing import HexStr
from web3.constants import ADDRESS_ZERO

from rocketwatch.plugins.log_events import event_definitions as defs
from rocketwatch.plugins.log_events.event_definitions import EVENT_REGISTRY, LogEvent
from rocketwatch.utils.chain_event import posted_name
from rocketwatch.utils.embeds import Embed
from tests.lib.explorer import stub_explorer_links
from tests.lib.scripted_rocketpool import ScriptedRocketPool, addr

ETH = 10**18
TX = HexStr("0x" + "ab" * 32)
BLOCK = 100
NODE = addr("0x" + "11" * 20)
OTHER = addr("0x" + "22" * 20)
MINIPOOL = addr("0x" + "33" * 20)
MEGAPOOL = addr("0x" + "44" * 20)
RECEIPT = {"from": NODE, "gasUsed": 21_000, "effectiveGasPrice": 10**9}


@pytest.fixture(autouse=True)
def _links(monkeypatch: pytest.MonkeyPatch, testnet_cfg: None) -> None:
    stub_explorer_links(monkeypatch, defs)


def _handler(contract: str, event: str) -> LogEvent:
    return EVENT_REGISTRY[contract][event]


def _args(**fields: Any) -> dict[str, Any]:
    return {
        "transactionHash": TX,
        "blockNumber": BLOCK,
        "event_name": "e",
        **fields,
    }


async def _build(
    handler: LogEvent, args: dict[str, Any], event: dict[str, Any] | None = None
) -> list[Embed]:
    return await handler.build_embeds(args, event or {}, RECEIPT)  # type: ignore[arg-type]


@dataclass
class Case:
    contract: str
    event: str
    args: dict[str, Any]
    # None for one-line (small) embeds
    title: str | None
    shows: list[str] = field(default_factory=list)
    image: bool = False

    def __str__(self) -> str:
        return f"{self.contract}.{self.event}"


MEMBER = {"nodeAddress": NODE}
MEGAPOOL_VALIDATOR = {"validatorId": 7, "node": NODE, "megapool": MEGAPOOL}

SIMPLE_EVENTS = [
    Case(
        "rocketNodeStaking",
        "RPLSlashed",
        {"amount": 500 * ETH, "ethValue": 5 * ETH, "node": NODE},
        ":rotating_light: Node Operator Slashed",
        ["**500 RPL** (5 ETH)", NODE],
        image=True,
    ),
    Case(
        "rocketDepositPool",
        "DepositRecycled",
        {"amount": 12 * ETH},
        None,
        ["**12 ETH** into the deposit pool"],
    ),
    Case(
        "rocketNodeDeposit",
        "DepositReceived",
        {"from": NODE, "amount": 8 * ETH},
        None,
        [NODE, "created a validator with a **8 ETH** bond"],
    ),
    Case(
        "rocketAuctionManager",
        "LotCreated",
        {"by": NODE, "rplAmount": 1_000 * ETH, "lotIndex": 3},
        ":scales: Lot Created",
        [NODE, "Lot #3", "1,000 RPL"],
    ),
    Case(
        "rocketAuctionManager",
        "RPLRecovered",
        {"rplAmount": 250 * ETH, "lotIndex": 3},
        ":scales: RPL Recovered From Lot",
        ["250 RPL recovered from Lot #3"],
    ),
    Case(
        "rocketDAOProtocol",
        "BootstrapSettingUint",
        {"settingPath": "deposit.fee", "value": 5},
        ":satellite_orbital: pDAO Bootstrap Mode: Setting Modified",
        ["Setting `deposit.fee` set to `5`"],
    ),
    Case(
        "rocketDAOProtocol",
        "BootstrapSpendTreasury",
        {"amount": 100 * ETH, "recipientAddress": OTHER},
        ":satellite_orbital: pDAO Bootstrap Mode: Treasury Spend",
        ["**100 RPL** from treasury sent to", OTHER],
    ),
    Case(
        "rocketDAOProtocol",
        "BootstrapSecurityInvite",
        {"memberAddress": OTHER, "id": "alice"},
        ":satellite_orbital: pDAO Bootstrap Mode: Security Council Invite",
        ["**alice**", OTHER],
    ),
    Case(
        "rocketDAOProtocol",
        "BootstrapSecurityKick",
        {"memberAddress": OTHER},
        ":satellite_orbital: pDAO Bootstrap Mode: Kicked Security Council Member",
        [OTHER, "removed from the security council"],
    ),
    Case(
        "rocketDAOProtocol",
        "BootstrapDisabled",
        {},
        ":satellite_orbital: pDAO Bootstrap Mode Disabled",
        ["on-chain governance"],
    ),
    Case(
        "rocketDAOProtocol",
        "BootstrapProtocolDAOEnabled",
        {},
        ":satellite_orbital: pDAO Bootstrap Mode: Enable Governance",
        ["On-chain governance has been enabled!"],
    ),
    Case(
        "rocketDAONodeTrustedActions",
        "ActionJoined",
        {**MEMBER, "rplBondAmount": 1_750 * ETH},
        ":new: oDAO Member Joined",
        [NODE, "bond of **1,750 RPL**"],
    ),
    Case(
        "rocketDAONodeTrustedActions",
        "ActionLeave",
        MEMBER,
        ":door: oDAO Member Left",
        [NODE, "left the oDAO"],
    ),
    Case(
        "rocketDAONodeTrustedActions",
        "ActionKick",
        MEMBER,
        ":boot: oDAO Member Kicked",
        [NODE, "kicked from the oDAO"],
    ),
    Case(
        "rocketDAOSecurityActions",
        "ActionJoined",
        MEMBER,
        ":new: Security Council Induction",
        [NODE, "has joined the security council"],
    ),
    Case(
        "rocketDAOSecurityActions",
        "ActionLeave",
        MEMBER,
        ":door: Security Council Resignation",
        [NODE, "has left the security council"],
    ),
    Case(
        "rocketDAOSecurityActions",
        "ActionRequestLeave",
        MEMBER,
        ":door: Security Council Resignation Request",
        [NODE, "requested to leave the security council"],
    ),
    Case(
        "rocketNetworkPenalties",
        "PenaltyUpdated",
        {"minipoolAddress": MINIPOOL, "penalty": ETH // 10},
        ":rotating_light: Minipool Penalty Updated",
        [MINIPOOL, "increased to 10%"],
        image=True,
    ),
    Case(
        "rocketMinipoolPenalty",
        "MaxPenaltyRateUpdated",
        {"rate": ETH // 2},
        ":rotating_light: Minipool Penalty",
        ["raised to 50%"],
        image=True,
    ),
    Case(
        "rocketMegapoolDelegate",
        "MegapoolValidatorExiting",
        MEGAPOOL_VALIDATOR,
        None,
        ["Validator 7 of node", NODE, "has started exiting"],
    ),
    Case(
        "rocketMegapoolDelegate",
        "MegapoolValidatorExited",
        MEGAPOOL_VALIDATOR,
        None,
        ["Validator 7 of node", NODE, "has exited"],
    ),
    Case(
        "rocketMegapoolDelegate",
        "MegapoolValidatorDissolved",
        {**MEGAPOOL_VALIDATOR, "from": NODE},
        ":rotating_light: Validator Dissolved",
        ["Validator 7 of node", NODE, "has been dissolved"],
        image=True,
    ),
    Case(
        "rocketMegapoolDelegate",
        "MegapoolPenaltyApplied",
        {"amount": 2 * ETH, "node": NODE, "megapool": MEGAPOOL, "from": NODE},
        ":police_car: Megapool Penalty Applied",
        [NODE, "penalized for **2 ETH**"],
        image=True,
    ),
]


class TestSimpleEvents:
    @pytest.mark.parametrize("case", SIMPLE_EVENTS, ids=str)
    async def test_renders_its_facts(self, case: Case) -> None:
        handler = _handler(case.contract, case.event)

        [embed] = await _build(handler, _args(**case.args))

        assert embed.title == case.title
        assert embed.description is not None
        for fragment in case.shows:
            assert fragment in embed.description
        assert bool(embed.image.url) is case.image
        assert posted_name(handler, [embed]) == handler.event_name

    @pytest.mark.parametrize(
        ("contract", "event", "member_arg"),
        [
            ("rocketDAONodeTrustedActions", "ActionLeave", "nodeAddress"),
            ("rocketDAONodeTrustedActions", "ActionKick", "nodeAddress"),
            ("rocketDAOSecurityActions", "ActionLeave", "nodeAddress"),
            ("rocketDAOSecurityActions", "ActionRequestLeave", "nodeAddress"),
            ("rocketDAOProtocol", "BootstrapSecurityKick", "memberAddress"),
        ],
    )
    async def test_departing_member_is_named_as_before_the_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        contract: str,
        event: str,
        member_arg: str,
    ) -> None:
        # once they're out, the member-name lookup comes back empty
        blocks: dict[str, Any] = {}

        async def link(target: str, *_: Any, block: Any = "latest", **__: Any) -> str:
            blocks[target] = block
            return target

        stub_explorer_links(monkeypatch, defs, link=link)

        await _build(_handler(contract, event), _args(**{member_arg: OTHER}))

        assert blocks[OTHER] == BLOCK - 1

    async def test_merkle_reward_claims_are_not_posted(self) -> None:
        handler = _handler("rocketMerkleDistributorMainnet", "RewardsClaimed")

        assert await _build(handler, _args()) == []


class TestDeposits:
    async def test_small_pool_deposit_is_a_one_liner(self) -> None:
        handler = _handler("rocketDepositPool", "DepositReceived")

        [embed] = await _build(handler, _args(**{"from": NODE, "amount": 10 * ETH}))

        assert embed.title is None
        assert embed.description is not None
        assert "deposited **10 ETH** for rETH" in embed.description

    @pytest.mark.parametrize(("amount", "image"), [(500, False), (1_500, True)])
    async def test_large_pool_deposit_gets_an_embed(
        self, amount: int, image: bool
    ) -> None:
        handler = _handler("rocketDepositPool", "DepositReceived")

        [embed] = await _build(handler, _args(**{"from": NODE, "amount": amount * ETH}))

        assert embed.title == ":rocket: Pool Deposit"
        assert bool(embed.image.url) is image

    @pytest.mark.parametrize(
        ("contract", "event", "args", "threshold", "title"),
        [
            (
                "rocketNodeDeposit",
                "DepositFor",
                {"from": OTHER, "nodeAddress": NODE},
                32,
                ":moneybag: Node ETH Deposit",
            ),
            (
                "rocketNodeDeposit",
                "Withdrawal",
                {"to": OTHER, "nodeAddress": NODE},
                100,
                ":leaves: Node ETH Withdrawal",
            ),
            (
                "rocketDepositPool",
                "CreditWithdrawn",
                {"nodeAddress": NODE},
                32,
                ":leaves: Credit Withdrawal",
            ),
        ],
    )
    async def test_node_balance_changes_get_an_embed_from_a_threshold(
        self,
        contract: str,
        event: str,
        args: dict[str, Any],
        threshold: int,
        title: str,
    ) -> None:
        handler = _handler(contract, event)

        [below] = await _build(handler, _args(**args, amount=(threshold - 1) * ETH))
        [at] = await _build(handler, _args(**args, amount=threshold * ETH))

        assert below.title is None
        assert below.description is not None
        assert NODE in below.description
        assert at.title == title

    @pytest.mark.parametrize(
        ("count", "name", "title"),
        [
            (1, "validator_deposit_event", None),
            (3, "validator_multi_deposit_event", None),
            (
                6,
                "validator_multi_deposit_event",
                ":construction_site: Multi Validator Deposit",
            ),
        ],
    )
    async def test_multi_validator_deposit(
        self, count: int, name: str, title: str | None
    ) -> None:
        handler = _handler("rocketNodeDeposit", "MultiDepositReceived")
        args = _args(
            **{"from": NODE, "numberOfValidators": count, "totalBond": 4 * count * ETH}
        )

        embeds = await _build(handler, args)

        [embed] = embeds
        assert posted_name(handler, embeds) == name
        assert embed.title == title
        assert embed.description is not None
        assert f"**{4 * count} ETH**" in embed.description


class TestSmoothingPool:
    @pytest.mark.parametrize(
        ("state", "name", "verb"),
        [
            (True, "node_smoothing_pool_joined", "joined"),
            (False, "node_smoothing_pool_left", "has left"),
        ],
    )
    async def test_counts_minipool_and_megapool_validators(
        self,
        scripted_rp: ScriptedRocketPool,
        state: bool,
        name: str,
        verb: str,
    ) -> None:
        scripted_rp.set_call("rocketMinipoolManager.getNodeMinipoolCount", 2)
        scripted_rp.set_call("rocketNodeManager.getMegapoolAddress", MEGAPOOL)
        scripted_rp.set_call("rocketMegapoolDelegate.getActiveValidatorCount", 5)
        handler = _handler("rocketNodeManager", "NodeSmoothingPoolStateChanged")

        embeds = await _build(handler, _args(node=NODE, state=state))

        [embed] = embeds
        assert posted_name(handler, embeds) == name
        assert embed.description is not None
        assert f"{verb} the smoothing pool with their 7 validators" in embed.description

    async def test_node_without_megapool(self, scripted_rp: ScriptedRocketPool) -> None:
        scripted_rp.set_call("rocketMinipoolManager.getNodeMinipoolCount", 2)
        scripted_rp.set_call("rocketNodeManager.getMegapoolAddress", ADDRESS_ZERO)
        handler = _handler("rocketNodeManager", "NodeSmoothingPoolStateChanged")

        [embed] = await _build(handler, _args(node=NODE, state=True))

        assert embed.description is not None
        assert "with their 2 validators" in embed.description


class TestMegapoolAssignments:
    @pytest.mark.parametrize(
        ("count", "text"),
        [(1, "Validator 7 of node"), (4, "**4 validators** of node")],
    )
    async def test_assignments_are_summarised(self, count: int, text: str) -> None:
        handler = _handler("rocketMegapoolDelegate", "MegapoolValidatorAssigned")

        [embed] = await _build(
            handler, _args(**MEGAPOOL_VALIDATOR), {"assignmentCount": count}
        )

        assert embed.description is not None
        assert text in embed.description


class TestStakingAndBurns:
    @pytest.mark.parametrize(
        ("amount", "posted", "title"),
        [(0.5, False, None), (50, True, None), (500, True, ":fire: rETH Burn")],
    )
    async def test_reth_burn_thresholds(
        self, amount: float, posted: bool, title: str | None
    ) -> None:
        handler = _handler("rocketTokenRETH", "TokensBurned")
        args = _args(
            **{
                "from": NODE,
                "amount": int(amount * ETH),
                "ethAmount": int(amount * ETH),
            }
        )

        embeds = await _build(handler, args)

        assert bool(embeds) is posted
        if posted:
            assert embeds[0].title == title

    @pytest.mark.parametrize(
        ("rpl", "posted", "title"),
        [
            (900, False, None),
            (1_200, True, None),
            (2_000, True, ":moneybag: RPL Stake"),
        ],
    )
    async def test_rpl_stake_thresholds(
        self,
        scripted_rp: ScriptedRocketPool,
        rpl: int,
        posted: bool,
        title: str | None,
    ) -> None:
        # at 0.005 ETH/RPL, a stake counts as large from 1,440 RPL
        scripted_rp.set_call("rocketNetworkPrices.getRPLPrice", ETH // 200)
        handler = _handler(
            "rocketNodeStaking", "RPLStaked(address,address,uint256,uint256)"
        )

        embeds = await _build(handler, _args(**{"from": NODE, "amount": rpl * ETH}))

        assert bool(embeds) is posted
        if posted:
            assert embeds[0].title == title


class TestNegativeRethRatio:
    @pytest.mark.parametrize(("total_eth", "posted"), [(1_100, False), (1_000, True)])
    async def test_only_decreases_are_posted(
        self, scripted_rp: ScriptedRocketPool, total_eth: int, posted: bool
    ) -> None:
        scripted_rp.set_call("rocketTokenRETH.getExchangeRate", ETH * 105 // 100)
        handler = _handler("rocketNetworkBalances", "BalancesUpdated")

        embeds = await _build(
            handler, _args(totalEth=total_eth * ETH, rethSupply=1_000 * ETH)
        )

        assert bool(embeds) is posted
        if posted:
            assert embeds[0].description is not None
            assert "from `1.05` to `1`" in embeds[0].description
