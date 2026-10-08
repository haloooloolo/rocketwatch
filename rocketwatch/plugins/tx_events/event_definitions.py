from __future__ import annotations

import datetime
from typing import Any, ClassVar, NotRequired, TypedDict

import humanize
from eth_typing import BlockNumber, ChecksumAddress, HexStr
from web3.types import TxData, TxReceipt

from rocketwatch.utils import solidity
from rocketwatch.utils.chain_event import ChainEvent, TemplateEvent
from rocketwatch.utils.dao import (
    build_claimer_description,
    decode_setting_multi,
)
from rocketwatch.utils.embeds import (
    Embed,
    el_explorer_url,
    format_value,
)
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.type_markers import (
    ContractAddress,
    NodeAddress,
    WalletAddress,
    Wei,
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
    timestamp: int


class TxEventData(TxData, total=False):
    """Transaction wrapper: all of :class:`TxData` plus an injected ``args`` key."""

    args: dict[str, Any]


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class TransactionEvent(ChainEvent[TxEventData]):
    """Base class for transaction event types, keyed by contract function."""

    # reported only when the transaction reverted (instead of only on success)
    reverted_only: ClassVar[bool] = False
    # the transaction executes a DAO proposal whose payload is reported too
    executes_payload: ClassVar[bool] = False

    # default args type — override in subclasses
    Args = EventContext


class TxTemplate(TemplateEvent[TxEventData], TransactionEvent):
    """A transaction event that is one sentence over its fields; see ``TemplateEvent``."""


YOURE_FIRED_GIF = (
    "https://media1.tenor.com/m/Xuv3IEoH1a4AAAAC/youre-fired-donald-trump.gif"
)


# ---------------------------------------------------------------------------
# Group 1: Simple one-off events
# ---------------------------------------------------------------------------


_bootstrap_odao_member = TxTemplate(
    "bootstrap_odao_member",
    "{nodeAddress} added as a new oDAO member!",
    fields={"nodeAddress": NodeAddress},
    title=":satellite_orbital: oDAO Bootstrap Mode: Member Added",
)


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
            await self.embed(
                args,
                title=":satellite_orbital: oDAO Bootstrap Mode Disabled",
                description=(
                    "Bootstrap mode for the oDAO is now disabled! The guardian has "
                    "handed off full control over the Oracle DAO to its members!"
                ),
            )
        ]


_odao_member_invite = TxTemplate(
    "odao_member_invite",
    "**{id}** ({nodeAddress}) has been invited to join the oDAO!",
    fields={"id": str, "nodeAddress": NodeAddress},
    title=":crystal_ball: oDAO Invite",
)


_sdao_member_invite = TxTemplate(
    "sdao_member_invite",
    "{memberAddress} has been invited to join the security council!",
    fields={"memberAddress": NodeAddress},
    title=":lock: Security Council Invite",
)


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
            await self.embed(
                args,
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
            await self.embed(
                args,
                title=self._title,
                description=f"Setting `{args['settingPath']}` set to `{value}`!",
                fields=fields or None,
            )
        ]


# ---------------------------------------------------------------------------
# Group 3: Proposal execute events (parameterized)
# ---------------------------------------------------------------------------


class ProposalExecuteEvent(TransactionEvent):
    executes_payload = True

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
            await self.embed(
                args,
                title=self._title,
                description=(
                    f"{fmt['executor']} executed **proposal #{args['proposalID']}**!\n"
                    f"```{args['proposal_body']}```"
                ),
            )
        ]


class DAOProposalExecuteEvent(TransactionEvent):
    """``rocketDAOProposal.execute``, shared by the oDAO and security council:
    resolves to the proposing DAO's execute event."""

    event_name = "dao_proposal_execute"

    class Args(EventContext):
        proposalID: int

    async def resolve(
        self, args: dict[str, Any], event: TxEventData
    ) -> TransactionEvent:
        dao_name: str = await rp.call("rocketDAOProposal.getDAO", args["proposalID"])
        return DAO_PROPOSAL_EVENTS[dao_name]

    async def build_embeds(
        self, args: Any, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        raise RuntimeError("Must be resolved first")


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
        startTime: int

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
            await self.embed(
                args,
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
                await self.embed(
                    args,
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
            await self.embed(
                args,
                title=":satellite_orbital: oDAO Bootstrap Mode: Network Upgrade",
                description=template.format(name=args["name"]),
            )
        ]


class PDAOSetDelegateEvent(TransactionEvent):
    event_name = "pdao_set_delegate"

    class Args(EventContext):
        # one of the two, depending on the function
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
                await self.embed(
                    args,
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
                await self.line(
                    args,
                    f":handshake: {delegator_clean} has delegated their voting "
                    f"power of **{power_str}** to {delegate_clean}!",
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
            await self.embed(
                args,
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
            await self.embed(
                args,
                title=":classical_building: Protocol DAO: Multiple Settings Modified",
                description=decode_setting_multi(args, args["data"]),
            )
        ]


_sdao_member_kick = TxTemplate(
    "sdao_member_kick",
    "{memberAddress} has been kicked from the security council!",
    fields={"memberAddress": ChecksumAddress},
    title=":boot: Security Council Expulsion",
    image=YOURE_FIRED_GIF,
    before=("memberAddress",),
)


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
        embed = await self.embed(
            args,
            title=":boot: Security Council Mass Expulsion",
            description=(
                f"Multiple members have been kicked from the security council!\n"
                f"{member_list}"
            ),
        )
        embed.set_image(url=YOURE_FIRED_GIF)
        return [embed]


_sdao_member_replace = TxTemplate(
    "sdao_member_replace",
    "{existingMemberAddress} has been replaced by {newMemberAddress}!",
    fields={"existingMemberAddress": ChecksumAddress, "newMemberAddress": NodeAddress},
    title=":repeat: Security Council Replacement",
    before=("existingMemberAddress",),
)


class FailedDepositEvent(TransactionEvent):
    event_name = "minipool_failed_deposit"
    reverted_only = True

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
            await self.embed(
                args,
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
    reloads_contracts = True

    def __init__(self, event_name: str, title: str, image_url: str) -> None:
        self.event_name = event_name
        self._title = title
        self._image_url = image_url

    async def build_embeds(
        self, args: Any, event: TxEventData, receipt: TxReceipt
    ) -> list[Embed]:
        embed = await self.embed(
            args,
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
        "bootstrapMember": _bootstrap_odao_member,
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
        "proposalInvite": _odao_member_invite,
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
        "proposalSecurityInvite": _sdao_member_invite,
        "proposalSecurityKick": _sdao_member_kick,
        "proposalSecurityKickMulti": SDAOMemberKickMultiEvent(),
        "proposalSecurityReplace": _sdao_member_replace,
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
