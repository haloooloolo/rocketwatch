from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from eth_typing import HexStr

from rocketwatch.plugins.tx_events import event_definitions as defs
from rocketwatch.plugins.tx_events.event_definitions import (
    TRANSACTION_REGISTRY,
    TransactionEvent,
)
from rocketwatch.utils.embeds import Embed
from tests.lib.explorer import stub_explorer_links
from tests.lib.scripted_rocketpool import ScriptedRocketPool, addr

ETH = 10**18
GWEI = 10**9
TX = HexStr("0x" + "ab" * 32)
NODE = addr("0x" + "11" * 20)
OTHER = addr("0x" + "22" * 20)

PROTOCOL = TRANSACTION_REGISTRY["rocketDAOProtocolProposals"]


@pytest.fixture(autouse=True)
def _links(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_explorer_links(monkeypatch, defs)


def _args(function_name: str = "f", **fields: Any) -> dict[str, Any]:
    return {
        "transactionHash": TX,
        "blockNumber": 100,
        "event_name": "e",
        "function_name": function_name,
        "timestamp": 0,
        **fields,
    }


async def _build(
    event: TransactionEvent,
    args: dict[str, Any],
    *,
    txn: dict[str, Any] | None = None,
    receipt: dict[str, Any] | None = None,
) -> list[Embed]:
    return await event.build_embeds(args, txn or {}, receipt or {})  # type: ignore[arg-type]


def _field(embed: Embed, name: str) -> str | None:
    return next((f.value for f in embed.fields if f.name == name), None)


class TestBootstrapDisable:
    async def test_unconfirmed_disable_is_not_posted(self) -> None:
        event = TRANSACTION_REGISTRY["rocketDAONodeTrusted"]["bootstrapDisable"]

        assert await _build(event, _args(confirmDisableBootstrapMode=False)) == []

    async def test_confirmed_disable_is_posted(self) -> None:
        event = TRANSACTION_REGISTRY["rocketDAONodeTrusted"]["bootstrapDisable"]

        [embed] = await _build(event, _args(confirmDisableBootstrapMode=True))

        assert embed.title is not None
        assert "Disabled" in embed.title


class TestSettingEvent:
    async def test_bool_setting_shows_boolean(self) -> None:
        args = _args(
            "proposalSettingBool",
            settingContractName="rocketDAOProtocolSettingsNode",
            settingPath="node.registration.enabled",
            value=1,
        )

        [embed] = await _build(PROTOCOL["proposalSettingBool"], args)

        assert embed.description == "Setting `node.registration.enabled` set to `True`!"
        assert _field(embed, "Contract") == "`rocketDAOProtocolSettingsNode`"

    async def test_uint_setting_shows_number(self) -> None:
        args = _args("proposalSettingUint", settingPath="deposit.fee", value=1)

        [embed] = await _build(PROTOCOL["proposalSettingUint"], args)

        assert embed.description == "Setting `deposit.fee` set to `1`!"
        assert _field(embed, "Contract") is None


class TestTreasurySpend:
    async def test_one_time_spend(self) -> None:
        args = _args(invoiceID="INV-7", recipientAddress=OTHER, amount=1500 * ETH)

        [embed] = await _build(PROTOCOL["proposalTreasuryOneTimeSpend"], args)

        assert embed.description is not None
        assert "**1,500 RPL**" in embed.description
        assert OTHER in embed.description
        assert _field(embed, "Invoice ID") == "`INV-7`"

    async def test_new_recurring_spend_shows_first_payment(self) -> None:
        args = _args(
            contractName="grants",
            recipientAddress=OTHER,
            amountPerPeriod=100 * ETH,
            periodLength=14 * 86400,
            numPeriods=12,
            startTime=1_700_000_000,
        )

        [embed] = await _build(PROTOCOL["proposalTreasuryNewContract"], args)

        assert embed.description is not None
        assert "**12 x 100 RPL**" in embed.description
        assert _field(embed, "Payment Interval") == "14 days"
        # paid at the end of the first period
        assert _field(embed, "First Payment") == f"<t:{1_700_000_000 + 14 * 86400}>"

    async def test_updated_recurring_spend_has_no_first_payment(self) -> None:
        args = _args(
            contractName="grants",
            recipientAddress=OTHER,
            amountPerPeriod=100 * ETH,
            periodLength=14 * 86400,
            numPeriods=12,
        )

        [embed] = await _build(PROTOCOL["proposalTreasuryUpdateContract"], args)

        assert _field(embed, "First Payment") is None


class TestTreasuryClaim:
    @staticmethod
    def _script_contracts(
        monkeypatch: pytest.MonkeyPatch,
        scripted_rp: ScriptedRocketPool,
        states: dict[str, tuple[int, int, int]],
    ) -> None:
        """name -> (numPeriods, periodsPaid before, periodsPaid after)"""

        async def get_function(path: str, name: str) -> Any:
            assert path == "rocketClaimDAO.getContract"
            num_periods, paid_before, paid_after = states[name]

            async def call(block_identifier: int) -> tuple[Any, ...]:
                paid = paid_before if block_identifier == 99 else paid_after
                return (OTHER, 50 * ETH, 7 * 86400, 0, num_periods, paid)

            return SimpleNamespace(call=call)

        monkeypatch.setattr(scripted_rp, "get_function", get_function, raising=False)

    @pytest.mark.parametrize(
        ("num_periods", "paid_after", "validity"),
        [
            (10, 10, "This was the final claim for this payment contract!"),
            (10, 9, "The contract is valid for one more period!"),
            (10, 7, "The contract is valid for 3 more periods."),
        ],
    )
    async def test_claim_reports_amount_and_remaining_periods(
        self,
        monkeypatch: pytest.MonkeyPatch,
        scripted_rp: ScriptedRocketPool,
        num_periods: int,
        paid_after: int,
        validity: str,
    ) -> None:
        self._script_contracts(
            monkeypatch,
            scripted_rp,
            {"grants": (num_periods, paid_after - 2, paid_after)},
        )
        event = TRANSACTION_REGISTRY["rocketClaimDAO"]["payOutContracts"]

        [embed] = await _build(event, _args(contractNames=["grants"]))

        assert embed.description is not None
        # two periods claimed in this transaction
        assert "**100 RPL** from `grants`" in embed.description
        assert embed.description.endswith(validity)
        assert _field(embed, "Payment Interval") == "7 days"

    async def test_one_embed_per_claimed_contract(
        self, monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
    ) -> None:
        self._script_contracts(
            monkeypatch,
            scripted_rp,
            {"grants": (10, 1, 2), "bounties": (10, 4, 5)},
        )
        event = TRANSACTION_REGISTRY["rocketClaimDAO"]["payOutContractsAndWithdraw"]

        embeds = await _build(event, _args(contractNames=["grants", "bounties"]))

        assert [("`grants`" in (e.description or "")) for e in embeds] == [True, False]
        assert len(embeds) == 2


class TestNetworkUpgrade:
    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            ("addContract", "Contract `rocketFoo` has been added!"),
            ("upgradeContract", "Contract `rocketFoo` has been upgraded!"),
            ("addABI", "for Contract `rocketFoo` has been added!"),
            ("upgradeABI", "of Contract `rocketFoo` has been upgraded!"),
        ],
    )
    async def test_describes_upgrade(self, kind: str, expected: str) -> None:
        event = TRANSACTION_REGISTRY["rocketDAONodeTrusted"]["bootstrapUpgrade"]

        [embed] = await _build(event, _args(type=kind, name="rocketFoo"))

        assert embed.description is not None
        assert embed.description.endswith(expected)

    async def test_unknown_upgrade_type_fails_loudly(self) -> None:
        event = TRANSACTION_REGISTRY["rocketDAONodeTrusted"]["bootstrapUpgrade"]

        with pytest.raises(Exception, match="not known"):
            await _build(event, _args(type="removeContract", name="rocketFoo"))


class TestDelegation:
    event = TRANSACTION_REGISTRY["rocketNetworkVoting"]["setDelegate"]

    async def test_small_voting_power_is_not_posted(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        scripted_rp.set_call("rocketNetworkVoting.getVotingPower", 49 * ETH)

        embeds = await _build(
            self.event, _args(newDelegate=OTHER), receipt={"from": NODE}
        )

        assert embeds == []

    async def test_self_delegation_is_not_posted(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        scripted_rp.set_call("rocketNetworkVoting.getVotingPower", 500 * ETH)

        embeds = await _build(
            self.event, _args(newDelegate=NODE), receipt={"from": NODE}
        )

        assert embeds == []

    async def test_medium_delegation_is_a_one_liner(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        scripted_rp.set_call("rocketNetworkVoting.getVotingPower", 120 * ETH)

        [embed] = await _build(
            self.event, _args(newDelegate=OTHER), receipt={"from": NODE}
        )

        assert embed.title is None
        assert embed.description is not None
        assert "**120**" in embed.description

    async def test_large_delegation_gets_a_full_embed(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        scripted_rp.set_call("rocketNetworkVoting.getVotingPower", 500 * ETH)
        initialise = TRANSACTION_REGISTRY["rocketNetworkVoting"][
            "initialiseVotingWithDelegate"
        ]

        [embed] = await _build(
            initialise, _args(delegate=OTHER), receipt={"from": NODE}
        )

        assert embed.title == ":handshake: Large pDAO Delegation"
        assert embed.description is not None
        assert NODE in embed.description
        assert OTHER in embed.description


DEPOSIT_TXN = {"gasPrice": 50 * GWEI}
DEPOSIT_RECEIPT = {"from": NODE, "gasUsed": 200_000}


class TestFailedDeposit:
    event = TRANSACTION_REGISTRY["rocketNodeDeposit"]["deposit"]

    async def test_reports_burned_gas_and_reason(
        self, monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
    ) -> None:
        monkeypatch.setattr(
            scripted_rp,
            "get_revert_reason",
            AsyncMock(return_value="Invalid amount"),
            raising=False,
        )

        [embed] = await _build(
            self.event, _args(), txn=DEPOSIT_TXN, receipt=DEPOSIT_RECEIPT
        )

        assert embed.description is not None
        assert "burned **0.01 ETH**" in embed.description
        assert _field(embed, "Likely Revert Reason") == "`Invalid amount`"

    async def test_unknown_reason_has_no_reason_field(
        self, monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
    ) -> None:
        monkeypatch.setattr(
            scripted_rp,
            "get_revert_reason",
            AsyncMock(return_value=""),
            raising=False,
        )

        [embed] = await _build(
            self.event, _args(), txn=DEPOSIT_TXN, receipt=DEPOSIT_RECEIPT
        )

        assert _field(embed, "Likely Revert Reason") is None

    async def test_insufficient_pre_deposit_is_not_posted(
        self, monkeypatch: pytest.MonkeyPatch, scripted_rp: ScriptedRocketPool
    ) -> None:
        # routine: the node's credit/balance check, not a lost deposit
        monkeypatch.setattr(
            scripted_rp,
            "get_revert_reason",
            AsyncMock(return_value="Deposit amount insufficient for pre deposit"),
            raising=False,
        )

        embeds = await _build(
            self.event, _args(), txn=DEPOSIT_TXN, receipt=DEPOSIT_RECEIPT
        )

        assert embeds == []


class TestProposalExecute:
    async def test_names_executor_and_proposal(self) -> None:
        args = _args(proposalID=42, executor=NODE, proposal_body="Raise the fee")

        [embed] = await _build(PROTOCOL["execute"], args)

        assert embed.description is not None
        assert "**proposal #42**" in embed.description
        assert NODE in embed.description
        assert "Raise the fee" in embed.description

    async def test_unresolved_dao_proposal_is_never_built(self) -> None:
        event = TRANSACTION_REGISTRY["rocketDAOProposal"]["execute"]

        with pytest.raises(RuntimeError):
            await _build(event, _args(proposalID=1))


class TestSimpleEvents:
    @pytest.mark.parametrize(
        ("contract", "function", "args", "title", "shows"),
        [
            (
                "rocketDAONodeTrusted",
                "bootstrapMember",
                {"nodeAddress": NODE},
                ":satellite_orbital: oDAO Bootstrap Mode: Member Added",
                [NODE, "added as a new oDAO member"],
            ),
            (
                "rocketDAONodeTrustedProposals",
                "proposalInvite",
                {"id": "alice", "nodeAddress": NODE},
                ":crystal_ball: oDAO Invite",
                ["**alice**", NODE, "invited to join the oDAO"],
            ),
            (
                "rocketDAOProtocolProposals",
                "proposalSecurityInvite",
                {"memberAddress": NODE},
                ":lock: Security Council Invite",
                [NODE, "invited to join the security council"],
            ),
            (
                "rocketDAOProtocolProposals",
                "proposalSecurityKick",
                {"memberAddress": NODE},
                ":boot: Security Council Expulsion",
                [NODE, "kicked from the security council"],
            ),
            (
                "rocketDAOProtocolProposals",
                "proposalSecurityReplace",
                {"existingMemberAddress": NODE, "newMemberAddress": OTHER},
                ":repeat: Security Council Replacement",
                [NODE, "has been replaced by", OTHER],
            ),
        ],
    )
    async def test_renders_its_facts(
        self,
        contract: str,
        function: str,
        args: dict[str, Any],
        title: str,
        shows: list[str],
    ) -> None:
        handler = TRANSACTION_REGISTRY[contract][function]

        [embed] = await _build(handler, _args(**args))

        assert embed.title == title
        assert embed.description is not None
        for fragment in shows:
            assert fragment in embed.description

    @pytest.mark.parametrize(
        ("function", "args"),
        [
            ("proposalSecurityKick", {"memberAddress": OTHER}),
            (
                "proposalSecurityReplace",
                {"existingMemberAddress": OTHER, "newMemberAddress": NODE},
            ),
        ],
    )
    async def test_departing_member_is_named_as_before_the_event(
        self, monkeypatch: pytest.MonkeyPatch, function: str, args: dict[str, Any]
    ) -> None:
        blocks: dict[str, Any] = {}

        async def link(target: str, *_: Any, block: Any = "latest", **__: Any) -> str:
            blocks[target] = block
            return target

        stub_explorer_links(monkeypatch, defs, link=link)

        await _build(PROTOCOL[function], _args(**args))

        assert blocks[OTHER] == 99
