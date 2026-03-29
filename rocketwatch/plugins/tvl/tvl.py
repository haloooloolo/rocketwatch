import logging
from typing import Any

import humanize
from colorama import Style
from discord import Interaction
from discord.app_commands import command, describe
from discord.ext.commands import Cog

from rocketwatch import RocketWatch
from utils import solidity
from utils.embeds import Embed
from utils.readable import render_tree
from utils.rocketpool import rp
from utils.shared_w3 import w3
from utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.tvl")


def minipool_split_rewards_logic(
    balance: float, node_share: float, commission: float, force_base: bool = False
) -> dict:
    d = {"base": {"reth": 0.0, "node": 0.0}, "rewards": {"reth": 0.0, "node": 0.0}}
    node_balance = 32 * node_share
    reth_balance = 32 - node_balance
    if balance >= 8 or force_base:
        # reth base share
        d["base"]["reth"] = min(balance, reth_balance)
        balance -= d["base"]["reth"]
        # node base share
        d["base"]["node"] = min(balance, node_balance)
        balance -= d["base"]["node"]
    # rewards split logic
    if balance > 0:
        node_ownership_share = node_share + (1 - node_share) * commission
        d["rewards"]["node"] = balance * node_ownership_share
        d["rewards"]["reth"] = balance * (1 - node_ownership_share)
    return d


def megapool_split_rewards(
    rewards: float,
    capital_ratio: float,
    node_commission: float,
    voter_share: float,
    dao_share: float,
) -> dict:
    borrowed_portion = rewards * (1 - capital_ratio)
    reth_commission = 1 - node_commission - voter_share - dao_share
    reth = borrowed_portion * reth_commission
    voter = borrowed_portion * voter_share
    dao = borrowed_portion * dao_share
    node = rewards - reth - voter - dao
    return {"node": node, "reth": reth, "voter": voter, "dao": dao}


class TVL(Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    @command()
    @describe(show_all="Also show entries with 0 value")
    async def tvl(self, interaction: Interaction, show_all: bool = False) -> None:
        """
        Show the total value locked in the protocol
        """
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        data: dict[str, Any] = {
            "Total RPL Locked": {
                "Staked RPL": {
                    "Minipools": {},  # accurate, live
                    "Megapools": {},  # accurate, live
                    "oDAO Bond": {},  # accurate, live
                },
                "Unclaimed Rewards": {
                    "Node Operators & oDAO": {},  # accurate, live
                    "pDAO": {},  # accurate, live
                },
                "Slashed RPL": {},  # accurate, live
                "Unused Inflation": {},  # accurate, live
            },
            "Total ETH Locked": {
                "Minipool Stake": {
                    "Dissolved Minipools": {
                        "Locked on Beacon Chain": {},  # accurate, db
                        "Contract Balance": {},  # accurate, db
                    },
                    "Staking Minipools": {
                        "rETH Share": {"_val": 0},  # done, db
                        "Node Share": {"_val": 0},  # done, db
                    },
                },
                "Megapool Stake": {
                    "Pending Validators": {},
                    "Dissolved Validators": {},
                    "Staking Validators": {
                        "rETH Share": {"_val": 0},
                        "Node Share": {"_val": 0},
                    },
                    "Exiting Validators": {
                        "rETH Share": {"_val": 0},
                        "Node Share": {"_val": 0},
                    },
                },
                "rETH Collateral": {
                    "Deposit Pool": {},  # accurate, live
                    "Extra Collateral": {},  # accurate, live
                },
                "Undistributed Balances": {
                    "Smoothing Pool Balance": {
                        "rETH Share": {"_val": 0},
                        "Node Share": {"_val": 0},
                    },
                    "Node Distributor Contracts": {
                        "rETH Share": {"_val": 0},  # done, db
                        "Node Share": {"_val": 0},  # done, db
                    },
                    "Minipool Contract Balances": {
                        "rETH Share": {"_val": 0},  # done, db
                        "Node Share": {"_val": 0},  # done, db
                    },
                    "Megapool Contract Balances": {
                        "rETH Share": {"_val": 0},
                        "Node Share": {"_val": 0},
                        "Voter Share": {"_val": 0},
                        "DAO Share": {"_val": 0},
                    },
                    "Beacon Chain Rewards": {
                        "rETH Share": {"_val": 0},
                        "Node Share": {"_val": 0},
                        "Voter Share": {"_val": 0},
                        "DAO Share": {"_val": 0},
                    },
                },
                "Unclaimed Rewards": {},  # accurate, live
            },
        }
        # note: _value in each dict will store the final string that gets rendered in the render

        eth_price = await rp.get_eth_usdc_price()
        rpl_price = solidity.to_float(await rp.call("rocketNetworkPrices.getRPLPrice"))
        rpl_address = await rp.get_address_by_name("rocketTokenRPL")

        # Dissolved Minipools:
        # Minipools that are flagged as dissolved are Pending minipools that didn't
        # trigger the second phase within the configured
        # LaunchTimeout (14 days at the time of writing).
        # They have the following applied to them:
        # - They have 1 ETH locked on the Beacon Chain, not earning any rewards.
        # - The 31 ETH that was waiting in their address was moved back to the Deposit Pool (This can cause the Deposit Pool
        #   to grow beyond its Cap, check the below comment for information about that).
        tmp = await (
            await self.bot.db.minipools.aggregate(
                [
                    {"$match": {"status": "dissolved", "vacant": False}},
                    {
                        "$group": {
                            "_id": "total",
                            "beacon_balance": {"$sum": "$beacon.balance"},
                            "execution_balance": {"$sum": "$execution_balance"},
                        }
                    },
                ]
            )
        ).to_list(1)
        if len(tmp) > 0:
            tmp = tmp[0]
            data["Total ETH Locked"]["Minipool Stake"]["Dissolved Minipools"][
                "Locked on Beacon Chain"
            ]["_val"] = tmp["beacon_balance"]
            data["Total ETH Locked"]["Minipool Stake"]["Dissolved Minipools"][
                "Contract Balance"
            ]["_val"] = tmp["execution_balance"]

        # Staking Minipools:
        minipools = await self.bot.db.minipools.find(
            {
                "status": {"$nin": ["initialised", "prelaunch", "dissolved"]},
                "node_deposit_balance": {"$exists": True},
            }
        ).to_list(None)

        for minipool in minipools:
            node_share = minipool["node_deposit_balance"] / 32
            commission = minipool["node_fee"]
            refund_balance = minipool["node_refund_balance"]
            contract_balance = minipool["execution_balance"]
            beacon_balance = (
                minipool["beacon"]["balance"] if "beacon" in minipool else 32
            )
            # if there is a refund_balance, we first try to pay that off using the contract balance
            if refund_balance > 0:
                if contract_balance > 0:
                    if contract_balance >= refund_balance:
                        contract_balance -= refund_balance
                        data["Total ETH Locked"]["Undistributed Balances"][
                            "Minipool Contract Balances"
                        ]["Node Share"]["_val"] += refund_balance
                        refund_balance = 0
                    else:
                        refund_balance -= contract_balance
                        data["Total ETH Locked"]["Undistributed Balances"][
                            "Minipool Contract Balances"
                        ]["Node Share"]["_val"] += contract_balance
                        contract_balance = 0
                # if there is still a refund balance, we try to pay it off using the beacon balance
                if refund_balance > 0 and beacon_balance > 0:
                    if beacon_balance >= refund_balance:
                        beacon_balance -= refund_balance
                        data["Total ETH Locked"]["Minipool Stake"]["Staking Minipools"][
                            "Node Share"
                        ]["_val"] += refund_balance
                        refund_balance = 0
                    else:
                        refund_balance -= beacon_balance
                        data["Total ETH Locked"]["Minipool Stake"]["Staking Minipools"][
                            "Node Share"
                        ]["_val"] += beacon_balance
                        beacon_balance = 0
            if beacon_balance > 0:
                d = minipool_split_rewards_logic(
                    beacon_balance, node_share, commission, force_base=True
                )
                data["Total ETH Locked"]["Minipool Stake"]["Staking Minipools"][
                    "Node Share"
                ]["_val"] += d["base"]["node"]
                data["Total ETH Locked"]["Minipool Stake"]["Staking Minipools"][
                    "rETH Share"
                ]["_val"] += d["base"]["reth"]
                data["Total ETH Locked"]["Undistributed Balances"][
                    "Beacon Chain Rewards"
                ]["Node Share"]["_val"] += d["rewards"]["node"]
                data["Total ETH Locked"]["Undistributed Balances"][
                    "Beacon Chain Rewards"
                ]["rETH Share"]["_val"] += d["rewards"]["reth"]
            if contract_balance > 0:
                d = minipool_split_rewards_logic(
                    contract_balance, node_share, commission
                )
                data["Total ETH Locked"]["Undistributed Balances"][
                    "Minipool Contract Balances"
                ]["Node Share"]["_val"] += d["base"]["node"] + d["rewards"]["node"]
                data["Total ETH Locked"]["Undistributed Balances"][
                    "Minipool Contract Balances"
                ]["rETH Share"]["_val"] += d["base"]["reth"] + d["rewards"]["reth"]

        # Megapool commission settings
        network_settings = await rp.get_contract_by_name(
            "rocketDAOProtocolSettingsNetwork"
        )
        node_share = solidity.to_float(
            await network_settings.functions.getNodeShare().call()
        )
        voter_share = solidity.to_float(
            await network_settings.functions.getVoterShare().call()
        )
        dao_share = solidity.to_float(
            await network_settings.functions.getProtocolDAOShare().call()
        )

        # Pending Megapool Validators: prestaked validators have deposit_value locked
        # (1 ETH on beacon + 31 ETH in contract as assignedValue)
        # in_queue validators are skipped — their ETH is in the Deposit Pool (already counted)
        tmp = await (
            await self.bot.db.megapool_validators.aggregate(
                [{"$match": {"status": "prestaked"}}, {"$count": "count"}]
            )
        ).to_list(1)
        if tmp:
            data["Total ETH Locked"]["Megapool Stake"]["Pending Validators"]["_val"] = (
                tmp[0]["count"] * 32
            )

        # Dissolved Megapool Validators: 1 ETH stuck on beacon chain, 31 ETH returned to DP
        tmp = await (
            await self.bot.db.megapool_validators.aggregate(
                [
                    {"$match": {"status": "dissolved"}},
                    {
                        "$group": {
                            "_id": "total",
                            "beacon_balance": {"$sum": "$beacon.balance"},
                        }
                    },
                ]
            )
        ).to_list(1)
        if tmp:
            data["Total ETH Locked"]["Megapool Stake"]["Dissolved Validators"][
                "_val"
            ] = tmp[0]["beacon_balance"]

        # Staking, Locked & Exiting Megapool Validators: beacon balance split by capital ratio
        # locked = exit requested but not yet confirmed on beacon chain, treated as exiting
        megapool_validators = await self.bot.db.megapool_validators.find(
            {"status": {"$in": ["staking", "locked", "exiting"]}}
        ).to_list(None)
        for v in megapool_validators:
            capital_ratio = v["requested_bond"] / 32
            beacon_balance = v.get("beacon", {}).get("balance", 32)
            status = v["status"]
            # base stake (up to 32 ETH)
            base = min(beacon_balance, 32)
            node_base = v["requested_bond"]
            # handle penalties (beacon < 32): node absorbs losses first
            if base < 32:
                shortfall = 32 - base
                node_base = max(0, node_base - shortfall)
            reth_base = base - node_base
            target = (
                "Staking Validators" if (status == "staking") else "Exiting Validators"
            )
            data["Total ETH Locked"]["Megapool Stake"][target]["rETH Share"][
                "_val"
            ] += reth_base
            data["Total ETH Locked"]["Megapool Stake"][target]["Node Share"][
                "_val"
            ] += node_base
            # beacon chain rewards (anything over 32)
            if beacon_balance > 32:
                rewards = beacon_balance - 32
                split = megapool_split_rewards(
                    rewards, capital_ratio, node_share, voter_share, dao_share
                )
                data["Total ETH Locked"]["Undistributed Balances"][
                    "Beacon Chain Rewards"
                ]["Node Share"]["_val"] += split["node"]
                data["Total ETH Locked"]["Undistributed Balances"][
                    "Beacon Chain Rewards"
                ]["rETH Share"]["_val"] += split["reth"]
                data["Total ETH Locked"]["Undistributed Balances"][
                    "Beacon Chain Rewards"
                ]["Voter Share"]["_val"] += split["voter"]
                data["Total ETH Locked"]["Undistributed Balances"][
                    "Beacon Chain Rewards"
                ]["DAO Share"]["_val"] += split["dao"]

        # Megapool Contract Balances: eth_balance = assignedValue + refundValue + pendingRewards
        # assignedValue already counted in Queued Validators, so we split the rest:
        #   refundValue (minus debt) → Node Share
        #   pendingRewards → split by commission (node/rETH/voter/DAO)
        megapool_balances = await (
            await self.bot.db.node_operators.aggregate(
                [
                    {
                        "$match": {
                            "megapool.deployed": True,
                            "megapool.eth_balance": {"$gt": 0},
                        }
                    },
                    {
                        "$project": {
                            "refund_value": "$megapool.refund_value",
                            "debt": "$megapool.debt",
                            "pending_rewards": "$megapool.pending_rewards",
                            "node_bond": "$megapool.node_bond",
                            "user_capital": "$megapool.user_capital",
                        }
                    },
                ]
            )
        ).to_list()
        for mp in megapool_balances:
            refund_value = mp.get("refund_value", 0)
            debt_val = mp.get("debt", 0)
            pending_rewards = mp.get("pending_rewards", 0)
            # refundValue minus debt → Node Share
            node_refund = max(0, refund_value - debt_val)
            data["Total ETH Locked"]["Undistributed Balances"][
                "Megapool Contract Balances"
            ]["Node Share"]["_val"] += node_refund
            # pendingRewards → split by commission
            if pending_rewards > 0:
                total_capital = mp.get("node_bond", 0) + mp.get("user_capital", 0)
                capital_ratio = (
                    mp.get("node_bond", 0) / total_capital if total_capital > 0 else 0
                )
                split = megapool_split_rewards(
                    pending_rewards, capital_ratio, node_share, voter_share, dao_share
                )
                data["Total ETH Locked"]["Undistributed Balances"][
                    "Megapool Contract Balances"
                ]["Node Share"]["_val"] += split["node"]
                data["Total ETH Locked"]["Undistributed Balances"][
                    "Megapool Contract Balances"
                ]["rETH Share"]["_val"] += split["reth"]
                data["Total ETH Locked"]["Undistributed Balances"][
                    "Megapool Contract Balances"
                ]["Voter Share"]["_val"] += split["voter"]
                data["Total ETH Locked"]["Undistributed Balances"][
                    "Megapool Contract Balances"
                ]["DAO Share"]["_val"] += split["dao"]

        # Deposit Pool Balance: calls the contract and asks what its balance is, simple enough.
        # ETH in here has been swapped for rETH and is waiting to be matched with a minipool.
        # Fun Fact: This value can go above the configured Deposit Pool Cap in 2 scenarios:
        #  - A Minipool gets dissolved, moving 16 ETH from its address back to the Deposit Pool.
        #  - ETH from withdrawn Minipools, which gets stored in the rETH contract,
        #    surpasses the configured targetCollateralRate,
        #    which is 10% at the time of writing. Once this occurs the ETH gets moved
        #    from the rETH contract to the Deposit Pool.
        data["Total ETH Locked"]["rETH Collateral"]["Deposit Pool"]["_val"] = (
            solidity.to_float(await rp.call("rocketDepositPool.getBalance"))
        )

        # Extra Collateral: This is ETH stored in the rETH contract from Minipools that have been withdrawn from.
        # This value has a cap - read the above comment for more information about that.
        data["Total ETH Locked"]["rETH Collateral"]["Extra Collateral"]["_val"] = (
            solidity.to_float(
                await w3.eth.get_balance(
                    await rp.get_address_by_name("rocketTokenRETH")
                )
            )
        )

        # Smoothing Pool Balance: This is ETH from Proposals by minipools that have joined the Smoothing Pool.
        smoothie_balance = solidity.to_float(
            await w3.eth.get_balance(
                await rp.get_address_by_name("rocketSmoothingPool")
            )
        )
        data["Total ETH Locked"]["Undistributed Balances"]["Smoothing Pool Balance"][
            "_val"
        ] = smoothie_balance

        # Unclaimed Smoothing Pool Rewards: This is ETH from the previous Reward Periods that have not been claimed yet.
        data["Total ETH Locked"]["Unclaimed Rewards"]["_val"] = solidity.to_float(
            await rp.call("rocketVault.balanceOf", "rocketMerkleDistributorMainnet")
        )

        # Staked RPL: This is all ETH that has been staked by node operators.
        data["Total RPL Locked"]["Staked RPL"]["Minipools"]["_val"] = solidity.to_float(
            await rp.call("rocketNodeStaking.getTotalLegacyStakedRPL")
        )
        data["Total RPL Locked"]["Staked RPL"]["Megapools"]["_val"] = solidity.to_float(
            await rp.call("rocketNodeStaking.getTotalMegapoolStakedRPL")
        )

        # oDAO bonded RPL: RPL oDAO Members have to lock up to join it. This RPL can be slashed if they misbehave.
        data["Total RPL Locked"]["Staked RPL"]["oDAO Bond"]["_val"] = solidity.to_float(
            await rp.call(
                "rocketVault.balanceOfToken", "rocketDAONodeTrustedActions", rpl_address
            )
        )

        # Unclaimed RPL Rewards: RPL rewards that have been earned by Node Operators but have not been claimed yet.
        data["Total RPL Locked"]["Unclaimed Rewards"]["Node Operators & oDAO"][
            "_val"
        ] = solidity.to_float(
            await rp.call(
                "rocketVault.balanceOfToken",
                "rocketMerkleDistributorMainnet",
                rpl_address,
            )
        )

        # Undistributed pDAO Rewards: RPL rewards that have been earned by the pDAO but have not been distributed yet.
        data["Total RPL Locked"]["Unclaimed Rewards"]["pDAO"]["_val"] = (
            solidity.to_float(
                await rp.call(
                    "rocketVault.balanceOfToken", "rocketClaimDAO", rpl_address
                )
            )
        )

        # Unused Inflation: RPL that has been minted but not yet been used for rewards.
        # This is (or was) an issue as the snapshots didn't account for the last day of inflation.
        # Joe is already looking into this.
        data["Total RPL Locked"]["Unused Inflation"]["_val"] = solidity.to_float(
            await rp.call(
                "rocketVault.balanceOfToken", "rocketRewardsPool", rpl_address
            )
        )

        # Slashed RPL: RPL that is slashed gets moved to the Auction Manager Contract.
        # This RPL will be sold using a Dutch Auction for ETH, which the gets moved to the rETH contract to be used as
        # extra rETH collateral.
        data["Total RPL Locked"]["Slashed RPL"]["_val"] = solidity.to_float(
            await rp.call(
                "rocketVault.balanceOfToken", "rocketAuctionManager", rpl_address
            )
        )

        # create _value string for each branch. the _value is the sum of all _val or _val values in the children
        tmp = await (
            await self.bot.db.node_operators.aggregate(
                [
                    {"$match": {"fee_distributor.eth_balance": {"$gt": 0}}},
                    {
                        "$project": {
                            "fee_distributor.eth_balance": 1,
                            "node_share": {
                                "$sum": [
                                    "$effective_node_share",
                                    {
                                        "$multiply": [
                                            {"$subtract": [1, "$effective_node_share"]},
                                            "$average_node_fee",
                                        ]
                                    },
                                ]
                            },
                        }
                    },
                    {
                        "$project": {
                            "node_share": {
                                "$multiply": [
                                    "$fee_distributor.eth_balance",
                                    "$node_share",
                                ]
                            },
                            "reth_share": {
                                "$multiply": [
                                    "$fee_distributor.eth_balance",
                                    {"$subtract": [1, "$node_share"]},
                                ]
                            },
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "node_share": {"$sum": "$node_share"},
                            "reth_share": {"$sum": "$reth_share"},
                        }
                    },
                ]
            )
        ).to_list()
        if len(tmp) > 0:
            data["Total ETH Locked"]["Undistributed Balances"][
                "Node Distributor Contracts"
            ]["Node Share"]["_val"] = tmp[0]["node_share"]
            data["Total ETH Locked"]["Undistributed Balances"][
                "Node Distributor Contracts"
            ]["rETH Share"]["_val"] = tmp[0]["reth_share"]

        def set_val_of_branch(branch: dict, unit: str) -> float:
            val = 0
            for child in branch:
                if isinstance(branch[child], dict):
                    branch[child]["_val"] = set_val_of_branch(branch[child], unit)
                    branch[child]["_value"] = f"{branch[child]['_val']:,.2f} {unit}"
                    if branch[child].get("_is_estimate", False):
                        branch[child]["_value"] = f"~{branch[child]['_value']}"
                    val += branch[child]["_val"]
                elif not child.startswith("_") or child == "_val":
                    val += branch[child]
            branch["_val"] = val
            branch["_value"] = f"{val:,.2f} {unit}"
            if branch.get("_is_estimate", False):
                branch["_value"] = f"~{branch['_value']}"
            return val

        set_val_of_branch(data["Total ETH Locked"], "ETH")
        set_val_of_branch(data["Total RPL Locked"], "RPL")
        # calculate total tvl
        total_tvl = data["Total ETH Locked"]["_val"] + (
            data["Total RPL Locked"]["_val"] * rpl_price
        )
        usdc_total_tvl = total_tvl * eth_price
        data["_value"] = f"{total_tvl:,.2f} ETH"
        test = render_tree(data, "Total Locked Value", max_depth=0 if show_all else 2)
        # send embed with tvl
        closer = f"or about {Style.BRIGHT}{humanize.intword(usdc_total_tvl, format='%.3f')} USDC{Style.RESET_ALL}".rjust(
            max([len(line) for line in test.split("\n")]) - 1
        )
        embed = Embed(title="Protocol TVL", description=f"```ansi\n{test}\n{closer}```")
        await interaction.followup.send(embed=embed)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(TVL(bot))
