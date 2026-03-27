from __future__ import annotations

import datetime
import math
from abc import ABC, abstractmethod
from typing import Any, ClassVar, NotRequired, TypedDict

import humanize
from eth_typing import ChecksumAddress
from web3.types import TxReceipt

from utils import solidity
from utils.dao import (
    build_claimer_description,
    decode_setting_multi,
    wrap_member_address,
)
from utils.embeds import Embed, el_explorer_url, finalize_embed
from utils.rocketpool import rp
from utils.shared_w3 import w3

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def format_value(value: int | float) -> str:
    """Format a numeric value for display: auto-decimal + comma separation."""
    if value:
        decimal = 5 - math.floor(math.log10(abs(value)))
        decimal = max(0, min(5, decimal))
        value = round(value, decimal)
    if value == int(value):
        value = int(value)
    return humanize.intcomma(value)


# ---------------------------------------------------------------------------
# TypedDicts — args passed to each event's ``build_embeds``.
# Each inherits from ``EventContext`` (fields injected by process_transaction)
# and adds the decoded calldata fields specific to that event.
# ---------------------------------------------------------------------------


class EventContext(TypedDict):
    """Fields injected into every args dict by ``process_transaction``."""

    transactionHash: str
    blockNumber: int
    event_name: str
    function_name: str
    timestamp: NotRequired[int]


class BootstrapMemberArgs(EventContext):
    nodeAddress: ChecksumAddress


class SettingArgs(EventContext):
    settingContractName: NotRequired[str]
    settingPath: str
    value: int | bool


class BootstrapDisableArgs(EventContext):
    confirmDisableBootstrapMode: bool


class BootstrapNetworkUpgradeArgs(EventContext):
    type: str
    name: str
    abi: str
    address: ChecksumAddress


class ProposalExecuteArgs(EventContext):
    proposalID: int
    executor: str
    proposal_body: str


class ODAOMemberInviteArgs(EventContext):
    id: str
    nodeAddress: ChecksumAddress


class SDAOMemberInviteArgs(EventContext):
    memberAddress: ChecksumAddress


class SDAOMemberKickArgs(EventContext):
    memberAddress: ChecksumAddress


class SDAOMemberKickMultiArgs(EventContext):
    memberAddresses: list[ChecksumAddress]


class SDAOMemberReplaceArgs(EventContext):
    existingMemberAddress: ChecksumAddress
    newMemberAddress: ChecksumAddress


class PDAOSettingMultiArgs(EventContext):
    settingContractNames: list[str]
    settingPaths: list[str]
    types: list[int]
    data: list[bytes]


class PDAOClaimerArgs(EventContext):
    nodePercent: int
    protocolPercent: int
    trustedNodePercent: int


class PDAOSetDelegateArgs(EventContext):
    delegate: NotRequired[ChecksumAddress]
    newDelegate: NotRequired[ChecksumAddress]


class PDAOSpendTreasuryArgs(EventContext):
    invoiceID: str
    recipientAddress: ChecksumAddress
    amount: int


class TreasuryRecurringSpendArgs(EventContext):
    contractName: str
    recipientAddress: ChecksumAddress
    amountPerPeriod: int
    periodLength: int
    numPeriods: int
    startTime: NotRequired[int]


class TreasuryRecurringClaimArgs(EventContext):
    contractNames: list[str]


#: Transaction wrapper: dict(TxData) + injected "args" key.
EventData = dict[str, Any]


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class TransactionEvent[ArgsT: EventContext](ABC):
    """Base class for transaction event types.

    Each subclass builds its own Discord embed(s) explicitly — no template
    lookup, no auto-transformation.  Return ``[]`` from ``build_embeds`` to
    filter the event out entirely.
    """

    event_name: str

    @abstractmethod
    async def build_embeds(
        self,
        args: ArgsT,
        event: EventData,
        receipt: TxReceipt | None,
    ) -> list[Embed]: ...


# ---------------------------------------------------------------------------
# Group 1: Simple one-off events
# ---------------------------------------------------------------------------


class BootstrapODAOMemberEvent(TransactionEvent[BootstrapMemberArgs]):
    event_name = "bootstrap_odao_member"

    async def build_embeds(
        self, args: BootstrapMemberArgs, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        node_link = await el_explorer_url(args["nodeAddress"])
        embed = Embed(title=":satellite_orbital: oDAO Bootstrap Mode: Member Added")
        embed.description = f"{node_link} added as a new oDAO member!"
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


class BootstrapODAODisableEvent(TransactionEvent[BootstrapDisableArgs]):
    event_name = "bootstrap_odao_disable"

    async def build_embeds(
        self, args: BootstrapDisableArgs, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        if not args["confirmDisableBootstrapMode"]:
            return []
        embed = Embed(title=":satellite_orbital: oDAO Bootstrap Mode Disabled")
        embed.description = (
            "Bootstrap mode for the oDAO is now disabled! The guardian has "
            "handed off full control over the Oracle DAO to its members!"
        )
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


class ODAOMemberInviteEvent(TransactionEvent[ODAOMemberInviteArgs]):
    event_name = "odao_member_invite"

    async def build_embeds(
        self, args: ODAOMemberInviteArgs, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        node_link = await el_explorer_url(args["nodeAddress"])
        embed = Embed(title=":crystal_ball: oDAO Invite")
        embed.description = (
            f"**{args['id']}** ({node_link}) has been invited to join the oDAO!"
        )
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


class SDAOMemberInviteEvent(TransactionEvent[SDAOMemberInviteArgs]):
    event_name = "sdao_member_invite"

    async def build_embeds(
        self, args: SDAOMemberInviteArgs, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        member_link = await el_explorer_url(args["memberAddress"])
        embed = Embed(title=":lock: Security Council Invite")
        embed.description = (
            f"{member_link} has been invited to join the security council!"
        )
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


class PDAOSpendTreasuryEvent(TransactionEvent[PDAOSpendTreasuryArgs]):
    event_name = "pdao_spend_treasury"

    async def build_embeds(
        self, args: PDAOSpendTreasuryArgs, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        amount = format_value(solidity.to_float(args["amount"]))
        recipient_link = await el_explorer_url(args["recipientAddress"])
        embed = Embed(title=":bank: DAO Treasury Spend")
        embed.description = f"**{amount} RPL** from treasury sent to {recipient_link}!"
        embed.add_field(name="Invoice ID", value=f"`{args['invoiceID']}`", inline=False)
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


# ---------------------------------------------------------------------------
# Group 2: Setting events (parameterized)
# ---------------------------------------------------------------------------


class SettingEvent(TransactionEvent[SettingArgs]):
    def __init__(self, event_name: str, title: str) -> None:
        self.event_name = event_name
        self._title = title

    async def build_embeds(
        self, args: SettingArgs, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        value = args["value"]
        if "SettingBool" in args["function_name"]:
            value = bool(value)
        embed = Embed(title=self._title)
        embed.description = f"Setting `{args['settingPath']}` set to `{value}`!"
        if "settingContractName" in args:
            embed.add_field(
                name="Contract",
                value=f"`{args['settingContractName']}`",
                inline=False,
            )
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


# ---------------------------------------------------------------------------
# Group 3: Proposal execute events (parameterized)
# ---------------------------------------------------------------------------


class ProposalExecuteEvent(TransactionEvent[ProposalExecuteArgs]):
    def __init__(self, event_name: str, title: str) -> None:
        self.event_name = event_name
        self._title = title

    async def build_embeds(
        self, args: ProposalExecuteArgs, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        executor_link = await el_explorer_url(args["executor"])
        embed = Embed(title=self._title)
        embed.description = (
            f"{executor_link} executed **proposal #{args['proposalID']}**!\n"
            f"```{args['proposal_body']}```"
        )
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


class DAOProposalExecuteEvent(TransactionEvent[EventContext]):
    """Placeholder for ``rocketDAOProposal.execute``.

    The DAO prefix (odao/sdao) is resolved by ``process_transaction`` which
    swaps this for the appropriate ``ProposalExecuteEvent`` instance.
    ``build_embeds`` should never be called directly.
    """

    event_name = "dao_proposal_execute"

    async def build_embeds(
        self, args: EventContext, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        raise RuntimeError(
            "DAOProposalExecuteEvent.build_embeds should not be called directly; "
            "process_transaction must resolve the DAO prefix first."
        )


# ---------------------------------------------------------------------------
# Group 4: Treasury recurring events
# ---------------------------------------------------------------------------


class TreasuryRecurringSpendEvent(TransactionEvent[TreasuryRecurringSpendArgs]):
    def __init__(self, event_name: str, title: str, *, has_start_time: bool) -> None:
        self.event_name = event_name
        self._title = title
        self._has_start_time = has_start_time

    async def build_embeds(
        self,
        args: TreasuryRecurringSpendArgs,
        event: EventData,
        receipt: TxReceipt | None,
    ) -> list[Embed]:
        amount_per_period = format_value(solidity.to_float(args["amountPerPeriod"]))
        recipient_link = await el_explorer_url(args["recipientAddress"])
        embed = Embed(title=self._title)
        embed.description = (
            f"{recipient_link} will be awarded "
            f"**{args['numPeriods']} x {amount_per_period} RPL**!"
        )
        embed.add_field(
            name="Payment Interval",
            value=humanize.naturaldelta(
                datetime.timedelta(seconds=args["periodLength"])
            ),
            inline=False,
        )
        if self._has_start_time:
            embed.add_field(
                name="First Payment",
                value=f"<t:{args['startTime'] + args['periodLength']}>",
                inline=False,
            )
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


class TreasuryRecurringClaimEvent(TransactionEvent[TreasuryRecurringClaimArgs]):
    event_name = "pdao_spend_treasury_recurring_claim"

    async def build_embeds(
        self,
        args: TreasuryRecurringClaimArgs,
        event: EventData,
        receipt: TxReceipt | None,
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
            recipient_address: str = contract_post[0]
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

            embed = Embed(title=":bank: DAO Treasury Contract Claim")
            embed.description = (
                f"{recipient_link} has claimed **{amount} RPL** "
                f"from `{contract_name}`!\n{validity}"
            )
            embed.add_field(
                name="Payment Interval",
                value=humanize.naturaldelta(datetime.timedelta(seconds=period_length)),
                inline=False,
            )
            embeds.append(
                await finalize_embed(
                    embed, args["transactionHash"], args["blockNumber"]
                )
            )
        return embeds


# ---------------------------------------------------------------------------
# Group 5: Events with custom enrichment
# ---------------------------------------------------------------------------


class BootstrapNetworkUpgradeEvent(TransactionEvent[BootstrapNetworkUpgradeArgs]):
    event_name = "bootstrap_odao_network_upgrade"

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
        args: BootstrapNetworkUpgradeArgs,
        event: EventData,
        receipt: TxReceipt | None,
    ) -> list[Embed]:
        template = self._DESCRIPTIONS.get(args["type"])
        if template is None:
            raise Exception(f"Network Upgrade of type {args['type']} is not known.")
        embed = Embed(title=":satellite_orbital: oDAO Bootstrap Mode: Network Upgrade")
        embed.description = template.format(name=args["name"])
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


class PDAOSetDelegateEvent(TransactionEvent[PDAOSetDelegateArgs]):
    event_name = "pdao_set_delegate"

    async def build_embeds(
        self, args: PDAOSetDelegateArgs, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        if receipt is None:
            receipt = await w3.eth.get_transaction_receipt(args["transactionHash"])
        delegator: str = receipt["from"]
        delegate: str = args.get("delegate", "") or args.get("newDelegate", "")
        voting_power = solidity.to_float(
            await rp.call(
                "rocketNetworkVoting.getVotingPower",
                delegator,
                args["blockNumber"],
            )
        )
        if (voting_power < 50) or (delegate == delegator):
            return []

        delegator_link = await el_explorer_url(delegator)
        delegate_link = await el_explorer_url(delegate)
        power_str = format_value(voting_power)

        if voting_power >= 200:
            embed = Embed(title=":handshake: Large pDAO Delegation")
            embed.description = (
                f"{delegator_link} has delegated their voting power of "
                f"**{power_str}** to {delegate_link}!"
            )
            return [
                await finalize_embed(
                    embed, args["transactionHash"], args["blockNumber"]
                )
            ]
        else:
            delegator_clean = await el_explorer_url(delegator, prefix=None)
            delegate_clean = await el_explorer_url(delegate, prefix=None)
            embed = Embed()
            embed.description = (
                f":handshake: {delegator_clean} has delegated their voting "
                f"power of **{power_str}** to {delegate_clean}!"
            )
            tx_link = await el_explorer_url(args["transactionHash"], name="[tnx]")
            embed.description += f" {tx_link}"
            embed.set_footer(text="")
            return [embed]


class PDAOClaimerEvent(TransactionEvent[PDAOClaimerArgs]):
    event_name = "pdao_claimer"

    async def build_embeds(
        self, args: PDAOClaimerArgs, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        description = build_claimer_description(args)
        embed = Embed(
            title=":classical_building: Protocol DAO: Changed Reward Distribution"
        )
        embed.description = f"```{description}```"
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


class PDAOSettingMultiEvent(TransactionEvent[PDAOSettingMultiArgs]):
    event_name = "pdao_setting_multi"

    async def build_embeds(
        self, args: PDAOSettingMultiArgs, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        description = decode_setting_multi(args, args["data"])
        embed = Embed(
            title=":classical_building: Protocol DAO: Multiple Settings Modified"
        )
        embed.description = description
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


class SDAOMemberKickEvent(TransactionEvent[SDAOMemberKickArgs]):
    event_name = "sdao_member_kick"

    async def build_embeds(
        self, args: SDAOMemberKickArgs, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        member_link = await wrap_member_address(
            args["memberAddress"], block=(args["blockNumber"] - 1)
        )
        embed = Embed(title=":boot: Security Council Expulsion")
        embed.description = f"{member_link} has been kicked from the security council!"
        embed.set_image(
            url="https://media1.tenor.com/m/Xuv3IEoH1a4AAAAC/youre-fired-donald-trump.gif"
        )
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


class SDAOMemberKickMultiEvent(TransactionEvent[SDAOMemberKickMultiArgs]):
    event_name = "sdao_member_kick_multi"

    async def build_embeds(
        self, args: SDAOMemberKickMultiArgs, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        block = args["blockNumber"] - 1
        member_links = [
            await wrap_member_address(addr, block=block)
            for addr in args["memberAddresses"]
        ]
        member_list = ", ".join(member_links)
        embed = Embed(title=":boot: Security Council Mass Expulsion")
        embed.description = (
            f"Multiple members have been kicked from the security council!\n"
            f"{member_list}"
        )
        embed.set_image(
            url="https://media1.tenor.com/m/Xuv3IEoH1a4AAAAC/youre-fired-donald-trump.gif"
        )
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


class SDAOMemberReplaceEvent(TransactionEvent[SDAOMemberReplaceArgs]):
    event_name = "sdao_member_replace"

    async def build_embeds(
        self, args: SDAOMemberReplaceArgs, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        existing_link = await wrap_member_address(
            args["existingMemberAddress"], block=(args["blockNumber"] - 1)
        )
        new_link = await el_explorer_url(args["newMemberAddress"])
        embed = Embed(title=":repeat: Security Council Replacement")
        embed.description = f"{existing_link} has been replaced by {new_link}!"
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


class FailedDepositEvent(TransactionEvent[EventContext]):
    event_name = "minipool_failed_deposit"

    async def build_embeds(
        self, args: EventContext, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        if receipt is None:
            receipt = await w3.eth.get_transaction_receipt(args["transactionHash"])

        reason: str = await rp.get_revert_reason(event)
        if "insufficient for pre deposit" in reason:
            return []

        node_link = await el_explorer_url(receipt["from"])
        burned = format_value(solidity.to_float(event["gasPrice"] * receipt["gasUsed"]))
        embed = Embed(title=":fire: Failed Validator Deposit")
        embed.description = (
            f":fire_engine: {node_link} burned **{burned} ETH** "
            f"trying to create a validator! :fire_engine:"
        )
        return [
            await finalize_embed(
                embed, args["transactionHash"], args["blockNumber"], reason=reason
            )
        ]


# ---------------------------------------------------------------------------
# Group 6: Upgrade events (parameterized)
# ---------------------------------------------------------------------------


class UpgradeTriggeredEvent(TransactionEvent[EventContext]):
    def __init__(self, event_name: str, title: str, image_url: str) -> None:
        self.event_name = event_name
        self._title = title
        self._image_url = image_url

    async def build_embeds(
        self, args: EventContext, event: EventData, receipt: TxReceipt | None
    ) -> list[Embed]:
        embed = Embed(title=self._title)
        embed.set_image(url=self._image_url)
        return [
            await finalize_embed(embed, args["transactionHash"], args["blockNumber"])
        ]


# ---------------------------------------------------------------------------
# Parameterized instances
# ---------------------------------------------------------------------------

# Settings
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

# Proposal execute
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
