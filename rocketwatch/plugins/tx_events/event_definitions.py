from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar, NotRequired, TypedDict

import humanize
from eth_typing import BlockNumber, HexStr
from web3.types import TxData, TxReceipt

from rocketwatch.utils import solidity
from rocketwatch.utils.dao import (
    build_claimer_description,
    decode_setting_multi,
)
from rocketwatch.utils.embeds import (
    Embed,
    build_event_embed,
    build_small_event_embed,
    el_explorer_url,
    format_value,
)
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.type_markers import (
    ContractAddress,
    NodeAddress,
    WalletAddress,
    Wei,
    auto_format,
)

# ---------------------------------------------------------------------------
# TypedDicts
# ---------------------------------------------------------------------------


class EventContext(TypedDict):
    """Fields injected into every args dict by ``process_transaction``."""

    transactionHash: HexStr
    blockNumber: BlockNumber
    event_name: str
    function_name: str
    timestamp: NotRequired[int]


class TxEventData(TxData, total=False):
    """Transaction wrapper: all of :class:`TxData` plus an injected ``args`` key."""

    args: dict[str, Any]


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class TransactionEvent(ABC):
    """Base class for transaction event types.

    Each subclass builds its own Discord embed(s) explicitly — no template
    lookup, no auto-transformation.  Return ``[]`` from ``build_embeds`` to
    filter the event out entirely.

    Subclasses should define a nested ``Args`` TypedDict to declare the
    expected fields and their formatting markers.
    """

    event_name: str

    class Args(EventContext):
        """Default args type — override in subclasses."""

    async def _fmt(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Auto-format *args* using this class's nested ``Args`` TypedDict."""
        return dict(await auto_format(args, type(self).Args))

    @abstractmethod
    async def build_embeds(
        self,
        args: Any,
        event: TxEventData,
        receipt: TxReceipt,
    ) -> list[Embed]: ...


# ---------------------------------------------------------------------------
# Group 1: Simple one-off events
# ---------------------------------------------------------------------------


class BootstrapODAOMemberEvent(TransactionEvent):
    event_name = "bootstrap_odao_member"

    class Args(EventContext):
        nodeAddress: NodeAddress

    async def build_embeds(
        self, args: Args, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":satellite_orbital: oDAO Bootstrap Mode: Member Added",
                description=f"{fmt['nodeAddress']} added as a new oDAO member!",
            )
        ]


class BootstrapODAODisableEvent(TransactionEvent):
    event_name = "bootstrap_odao_disable"

    class Args(EventContext):
        confirmDisableBootstrapMode: bool

    async def build_embeds(
        self, args: Args, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        if not args["confirmDisableBootstrapMode"]:
            return []
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":satellite_orbital: oDAO Bootstrap Mode Disabled",
                description=(
                    "Bootstrap mode for the oDAO is now disabled! The guardian has "
                    "handed off full control over the Oracle DAO to its members!"
                ),
            )
        ]


class ODAOMemberInviteEvent(TransactionEvent):
    event_name = "odao_member_invite"

    class Args(EventContext):
        id: str
        nodeAddress: NodeAddress

    async def build_embeds(
        self, args: Args, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":crystal_ball: oDAO Invite",
                description=(
                    f"**{args['id']}** ({fmt['nodeAddress']}) has been invited to join the oDAO!"
                ),
            )
        ]


class SDAOMemberInviteEvent(TransactionEvent):
    event_name = "sdao_member_invite"

    class Args(EventContext):
        memberAddress: NodeAddress

    async def build_embeds(
        self, args: Args, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":lock: Security Council Invite",
                description=(
                    f"{fmt['memberAddress']} has been invited to join the security council!"
                ),
            )
        ]


class PDAOSpendTreasuryEvent(TransactionEvent):
    event_name = "pdao_spend_treasury"

    class Args(EventContext):
        invoiceID: str
        recipientAddress: WalletAddress
        amount: Wei

    async def build_embeds(
        self, args: Args, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount = format_value(fmt["amount"])
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":bank: DAO Treasury Spend",
                description=f"**{amount} RPL** from treasury sent to {fmt['recipientAddress']}!",
                fields=[("Invoice ID", f"`{args['invoiceID']}`", False)],
            )
        ]


# ---------------------------------------------------------------------------
# Group 2: Setting events (parameterized)
# ---------------------------------------------------------------------------


class SettingEvent(TransactionEvent):
    class Args(EventContext):
        settingContractName: NotRequired[str]
        settingPath: str
        value: int | bool

    def __init__(self, event_name: str, title: str) -> None:
        self.event_name = event_name
        self._title = title

    async def build_embeds(
        self, args: Args, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        value = args["value"]
        if "SettingBool" in args["function_name"]:
            value = bool(value)
        fields = []
        if "settingContractName" in args:
            fields.append(("Contract", f"`{args['settingContractName']}`", False))
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=self._title,
                description=f"Setting `{args['settingPath']}` set to `{value}`!",
                fields=fields or None,
            )
        ]


# ---------------------------------------------------------------------------
# Group 3: Proposal execute events (parameterized)
# ---------------------------------------------------------------------------


class ProposalExecuteEvent(TransactionEvent):
    class Args(EventContext):
        proposalID: int
        executor: WalletAddress
        proposal_body: str

    def __init__(self, event_name: str, title: str) -> None:
        self.event_name = event_name
        self._title = title

    async def build_embeds(
        self, args: Args, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=self._title,
                description=(
                    f"{fmt['executor']} executed **proposal #{args['proposalID']}**!\n"
                    f"```{args['proposal_body']}```"
                ),
            )
        ]


class DAOProposalExecuteEvent(TransactionEvent):
    """Placeholder for ``rocketDAOProposal.execute``.

    The DAO prefix (odao/sdao) is resolved by ``process_transaction`` which
    swaps this for the appropriate ``ProposalExecuteEvent`` instance.
    ``build_embeds`` should never be called directly.
    """

    event_name = "dao_proposal_execute"

    async def build_embeds(
        self, args: Any, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        raise RuntimeError(
            "DAOProposalExecuteEvent.build_embeds should not be called directly; "
            "process_transaction must resolve the DAO prefix first."
        )


# ---------------------------------------------------------------------------
# Group 4: Treasury recurring events
# ---------------------------------------------------------------------------


class TreasuryRecurringSpendEvent(TransactionEvent):
    class Args(EventContext):
        contractName: str
        recipientAddress: WalletAddress
        amountPerPeriod: Wei
        periodLength: int
        numPeriods: int
        startTime: NotRequired[int]

    def __init__(self, event_name: str, title: str, *, has_start_time: bool) -> None:
        self.event_name = event_name
        self._title = title
        self._has_start_time = has_start_time

    async def build_embeds(
        self,
        args: Args,
        event: TxEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        fmt = await self._fmt(args)
        amount_per_period = format_value(fmt["amountPerPeriod"])
        fields: list[tuple[str, str, bool]] = [
            (
                "Payment Interval",
                humanize.naturaldelta(datetime.timedelta(seconds=args["periodLength"])),
                False,
            ),
        ]
        if self._has_start_time:
            fields.append(
                (
                    "First Payment",
                    f"<t:{args['startTime'] + args['periodLength']}>",
                    False,
                )
            )
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=self._title,
                description=(
                    f"{fmt['recipientAddress']} will be awarded "
                    f"**{args['numPeriods']} x {amount_per_period} RPL**!"
                ),
                fields=fields,
            )
        ]


class TreasuryRecurringClaimEvent(TransactionEvent):
    event_name = "pdao_spend_treasury_recurring_claim"

    class Args(EventContext):
        contractNames: list[str]

    async def build_embeds(
        self,
        args: Args,
        event: TxEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        embeds: list[Embed] = []
        for contract_name in args["contractNames"]:
            get_contract = await rp.get_function(
                "rocketClaimDAO.getContract", contract_name
            )
            contract_pre = await get_contract.call(
                block_identifier=(args["blockNumber"] - 1)
            )
            contract_post = await get_contract.call(
                block_identifier=args["blockNumber"]
            )

            period_length: int = contract_post[2]
            recipient_address: WalletAddress = contract_post[0]
            periods_claimed: int = contract_post[5] - contract_pre[5]
            amount = format_value(solidity.to_float(periods_claimed * contract_post[1]))

            recipient_link = await el_explorer_url(recipient_address)

            periods_left: int = contract_post[4] - contract_post[5]
            if periods_left == 0:
                validity = "This was the final claim for this payment contract!"
            elif periods_left == 1:
                validity = "The contract is valid for one more period!"
            else:
                validity = f"The contract is valid for {periods_left} more periods."

            embeds.append(
                await build_event_embed(
                    tx_hash=args["transactionHash"],
                    block_number=args["blockNumber"],
                    title=":bank: DAO Treasury Contract Claim",
                    description=(
                        f"{recipient_link} has claimed **{amount} RPL** "
                        f"from `{contract_name}`!\n{validity}"
                    ),
                    fields=[
                        (
                            "Payment Interval",
                            humanize.naturaldelta(
                                datetime.timedelta(seconds=period_length)
                            ),
                            False,
                        )
                    ],
                )
            )
        return embeds


# ---------------------------------------------------------------------------
# Group 5: Events with custom enrichment
# ---------------------------------------------------------------------------


class BootstrapNetworkUpgradeEvent(TransactionEvent):
    event_name = "bootstrap_odao_network_upgrade"

    class Args(EventContext):
        type: str
        name: str
        abi: str
        address: ContractAddress

    _DESCRIPTIONS: ClassVar[dict[str, str]] = {
        "addContract": "Contract `{name}` has been added!",
        "upgradeContract": "Contract `{name}` has been upgraded!",
        "addABI": (
            "[ABI](https://ethereum.org/en/glossary/#abi) for Contract"
            " `{name}` has been added!"
        ),
        "upgradeABI": (
            "[ABI](https://ethereum.org/en/glossary/#abi) of Contract"
            " `{name}` has been upgraded!"
        ),
    }

    async def build_embeds(
        self,
        args: Args,
        event: TxEventData,
        receipt: TxReceipt,
    ) -> list[Embed]:
        template = self._DESCRIPTIONS.get(args["type"])
        if template is None:
            raise Exception(f"Network Upgrade of type {args['type']} is not known.")
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":satellite_orbital: oDAO Bootstrap Mode: Network Upgrade",
                description=template.format(name=args["name"]),
            )
        ]


class PDAOSetDelegateEvent(TransactionEvent):
    event_name = "pdao_set_delegate"

    class Args(EventContext):
        delegate: NotRequired[NodeAddress]
        newDelegate: NotRequired[NodeAddress]

    async def build_embeds(
        self, args: Args, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        delegator: NodeAddress = receipt["from"]
        delegate: NodeAddress | None = args.get("delegate") or args.get("newDelegate")
        voting_power = solidity.to_float(
            await rp.call(
                "rocketNetworkVoting.getVotingPower",
                delegator,
                args["blockNumber"],
            )
        )
        if (voting_power < 50) or (delegate == delegator):
            return []

        assert delegate is not None

        delegator_link = await el_explorer_url(delegator)
        delegate_link = await el_explorer_url(delegate)
        power_str = format_value(voting_power)

        if voting_power >= 200:
            return [
                await build_event_embed(
                    tx_hash=args["transactionHash"],
                    block_number=args["blockNumber"],
                    title=":handshake: Large pDAO Delegation",
                    description=(
                        f"{delegator_link} has delegated their voting power of "
                        f"**{power_str}** to {delegate_link}!"
                    ),
                )
            ]
        else:
            delegator_clean = await el_explorer_url(delegator, prefix=None)
            delegate_clean = await el_explorer_url(delegate, prefix=None)
            return [
                await build_small_event_embed(
                    f":handshake: {delegator_clean} has delegated their voting "
                    f"power of **{power_str}** to {delegate_clean}!",
                    args["transactionHash"],
                )
            ]


class PDAOClaimerEvent(TransactionEvent):
    event_name = "pdao_claimer"

    class Args(EventContext):
        nodePercent: int
        protocolPercent: int
        trustedNodePercent: int

    async def build_embeds(
        self, args: Args, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":classical_building: Protocol DAO: Changed Reward Distribution",
                description=f"```{build_claimer_description(args)}```",
            )
        ]


class PDAOSettingMultiEvent(TransactionEvent):
    event_name = "pdao_setting_multi"

    class Args(EventContext):
        settingContractNames: list[str]
        settingPaths: list[str]
        types: list[int]
        data: list[bytes]

    async def build_embeds(
        self, args: Args, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":classical_building: Protocol DAO: Multiple Settings Modified",
                description=decode_setting_multi(args, args["data"]),
            )
        ]


class SDAOMemberKickEvent(TransactionEvent):
    event_name = "sdao_member_kick"

    class Args(EventContext):
        memberAddress: NodeAddress

    async def build_embeds(
        self, args: Args, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        member_link = await el_explorer_url(
            args["memberAddress"], block=(args["blockNumber"] - 1)
        )
        embed = await build_event_embed(
            tx_hash=args["transactionHash"],
            block_number=args["blockNumber"],
            title=":boot: Security Council Expulsion",
            description=f"{member_link} has been kicked from the security council!",
        )
        embed.set_image(
            url="https://media1.tenor.com/m/Xuv3IEoH1a4AAAAC/youre-fired-donald-trump.gif"
        )
        return [embed]


class SDAOMemberKickMultiEvent(TransactionEvent):
    event_name = "sdao_member_kick_multi"

    class Args(EventContext):
        memberAddresses: list[NodeAddress]

    async def build_embeds(
        self, args: Args, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        block = args["blockNumber"] - 1
        member_links = [
            await el_explorer_url(addr, block=block) for addr in args["memberAddresses"]
        ]
        member_list = ", ".join(member_links)
        embed = await build_event_embed(
            tx_hash=args["transactionHash"],
            block_number=args["blockNumber"],
            title=":boot: Security Council Mass Expulsion",
            description=(
                f"Multiple members have been kicked from the security council!\n"
                f"{member_list}"
            ),
        )
        embed.set_image(
            url="https://media1.tenor.com/m/Xuv3IEoH1a4AAAAC/youre-fired-donald-trump.gif"
        )
        return [embed]


class SDAOMemberReplaceEvent(TransactionEvent):
    event_name = "sdao_member_replace"

    class Args(EventContext):
        existingMemberAddress: NodeAddress
        newMemberAddress: NodeAddress

    async def build_embeds(
        self, args: Args, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        existing_link = await el_explorer_url(
            args["existingMemberAddress"], block=(args["blockNumber"] - 1)
        )
        fmt = await self._fmt(args)
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":repeat: Security Council Replacement",
                description=f"{existing_link} has been replaced by {fmt['newMemberAddress']}!",
            )
        ]


class FailedDepositEvent(TransactionEvent):
    event_name = "minipool_failed_deposit"

    async def build_embeds(
        self, args: Any, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        reason: str = await rp.get_revert_reason(event)
        if "insufficient for pre deposit" in reason:
            return []

        node_link = await el_explorer_url(receipt["from"])
        burned = format_value(solidity.to_float(event["gasPrice"] * receipt["gasUsed"]))
        fields = []
        if reason:
            fields.append(("Likely Revert Reason", f"`{reason}`", False))
        return [
            await build_event_embed(
                tx_hash=args["transactionHash"],
                block_number=args["blockNumber"],
                title=":fire: Failed Validator Deposit",
                description=(
                    f":fire_engine: {node_link} burned **{burned} ETH** "
                    f"trying to create a validator! :fire_engine:"
                ),
                fields=fields or None,
            )
        ]


# ---------------------------------------------------------------------------
# Group 6: Upgrade events (parameterized)
# ---------------------------------------------------------------------------


class UpgradeTriggeredEvent(TransactionEvent):
    def __init__(self, event_name: str, title: str, image_url: str) -> None:
        self.event_name = event_name
        self._title = title
        self._image_url = image_url

    async def build_embeds(
        self, args: Any, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        embed = await build_event_embed(
            tx_hash=args["transactionHash"],
            block_number=args["blockNumber"],
            title=self._title,
        )
        embed.set_image(url=self._image_url)
        return [embed]


# ---------------------------------------------------------------------------
# Shared parameterized instances (multi-use)
# ---------------------------------------------------------------------------

_bootstrap_odao_setting = SettingEvent(
    "bootstrap_odao_setting",
    ":satellite_orbital: oDAO Bootstrap Mode: Setting Modified",
)
_odao_setting = SettingEvent(
    "odao_setting",
    ":crystal_ball: oDAO Setting Modified",
)
_sdao_setting = SettingEvent(
    "sdao_setting",
    ":lock: Security Council Setting Modified",
)
_pdao_setting = SettingEvent(
    "pdao_setting",
    ":classical_building: Protocol DAO: Setting Modified",
)
_odao_proposal_execute = ProposalExecuteEvent(
    "odao_proposal_execute",
    ":white_check_mark: oDAO Proposal Executed",
)
_sdao_proposal_execute = ProposalExecuteEvent(
    "sdao_proposal_execute",
    ":white_check_mark: Security Council Proposal Executed",
)
_pdao_proposal_execute = ProposalExecuteEvent(
    "pdao_proposal_execute",
    ":white_check_mark: pDAO Proposal Executed",
)

# DAO prefix resolution lookup
DAO_PROPOSAL_EVENTS: dict[str, ProposalExecuteEvent] = {
    "rocketDAONodeTrustedProposals": _odao_proposal_execute,
    "rocketDAOSecurityProposals": _sdao_proposal_execute,
}


# ---------------------------------------------------------------------------
# Registry — single source of truth for (contract, function) → event
# ---------------------------------------------------------------------------

TRANSACTION_REGISTRY: dict[str, dict[str, TransactionEvent]] = {
    # Bootstrap oDAO
    "rocketDAONodeTrusted": {
        "bootstrapMember": BootstrapODAOMemberEvent(),
        "bootstrapSettingUint": _bootstrap_odao_setting,
        "bootstrapSettingBool": _bootstrap_odao_setting,
        "bootstrapUpgrade": BootstrapNetworkUpgradeEvent(),
        "bootstrapDisable": BootstrapODAODisableEvent(),
    },
    # DAO proposal (prefix resolved at runtime)
    "rocketDAOProposal": {
        "execute": DAOProposalExecuteEvent(),
    },
    # oDAO proposals
    "rocketDAONodeTrustedProposals": {
        "execute": _odao_proposal_execute,
        "proposalSettingUint": _odao_setting,
        "proposalSettingBool": _odao_setting,
        "proposalInvite": ODAOMemberInviteEvent(),
    },
    # Security council proposals
    "rocketDAOSecurityProposals": {
        "execute": _sdao_proposal_execute,
        "proposalSettingUint": _sdao_setting,
        "proposalSettingBool": _sdao_setting,
        "proposalSettingAddress": _sdao_setting,
    },
    # Protocol DAO proposals
    "rocketDAOProtocolProposal": {
        "execute": _pdao_proposal_execute,
    },
    "rocketDAOProtocolProposals": {
        "execute": _pdao_proposal_execute,
        "proposalSettingMulti": PDAOSettingMultiEvent(),
        "proposalSettingUint": _pdao_setting,
        "proposalSettingBool": _pdao_setting,
        "proposalSettingAddress": _pdao_setting,
        "proposalSettingRewardsClaimers": PDAOClaimerEvent(),
        "proposalTreasuryOneTimeSpend": PDAOSpendTreasuryEvent(),
        "proposalTreasuryNewContract": TreasuryRecurringSpendEvent(
            "pdao_spend_treasury_recurring_new",
            ":bank: DAO Treasury: New Recurring Spend",
            has_start_time=True,
        ),
        "proposalTreasuryUpdateContract": TreasuryRecurringSpendEvent(
            "pdao_spend_treasury_recurring_update",
            ":bank: DAO Treasury: Updated Recurring Spend",
            has_start_time=False,
        ),
        "proposalSecurityInvite": SDAOMemberInviteEvent(),
        "proposalSecurityKick": SDAOMemberKickEvent(),
        "proposalSecurityKickMulti": SDAOMemberKickMultiEvent(),
        "proposalSecurityReplace": SDAOMemberReplaceEvent(),
    },
    # Treasury claims
    "rocketClaimDAO": {
        "payOutContracts": TreasuryRecurringClaimEvent(),
        "payOutContractsAndWithdraw": TreasuryRecurringClaimEvent(),
    },
    # Voting delegation
    "rocketNetworkVoting": {
        "initialiseVotingWithDelegate": PDAOSetDelegateEvent(),
        "setDelegate": PDAOSetDelegateEvent(),
    },
    # Failed deposits
    "rocketNodeDeposit": {
        "deposit": FailedDepositEvent(),
        "depositWithCredit": FailedDepositEvent(),
    },
    # Protocol upgrades
    "rocketUpgradeOneDotOne": {
        "execute": UpgradeTriggeredEvent(
            "redstone_upgrade_triggered",
            ":tada: Redstone Upgrade Complete!",
            "https://cdn.dribbble.com/users/187497/screenshots/2284528/media/123903807d334c15aa105b44f2bd9252.gif",
        ),
    },
    "rocketUpgradeOneDotTwo": {
        "execute": UpgradeTriggeredEvent(
            "atlas_upgrade_triggered",
            ":tada: Atlas Upgrade Complete!",
            "https://cdn.discordapp.com/attachments/912434217118498876/1097528472567558227/"
            "DALLE_2023-04-17_16.25.46_-_an_expresive_oil_painting_of_the_atlas_2_rocket_taking_off_moon_colorfull.png",
        ),
    },
    "rocketUpgradeOneDotThree": {
        "execute": UpgradeTriggeredEvent(
            "houston_upgrade_triggered",
            ":tada: Houston Upgrade Complete!",
            "https://i.imgur.com/XT5qPWf.png",
        ),
    },
    "rocketUpgradeOneDotThreeDotOne": {
        "execute": UpgradeTriggeredEvent(
            "houston_hotfix_upgrade_triggered",
            ":tada: Houston Hotfix Upgrade Complete!",
            "https://i.imgur.com/JcQS3Sh.png",
        ),
    },
    "rocketUpgradeOneDotFour": {
        "execute": UpgradeTriggeredEvent(
            "saturn_one_upgrade_triggered",
            ":ringed_planet: Saturn 1 Upgrade Complete!",
            "https://i.imgur.com/n3wMCOA.png",
        ),
    },
}
