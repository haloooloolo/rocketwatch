import contextlib

from eth_typing import ChecksumAddress

from rocketwatch.utils import solidity
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.shared_w3 import w3

price_cache = {"block": 0, "rpl_price": 0, "reth_price": 0}

sea_creatures = {
    # 32 * 100: spouting whale emoji
    32 * 100: "🐳",
    # 32 * 50: whale emoji
    32 * 50: "🐋",
    # 32 * 30: shark emoji
    32 * 30: "🦈",
    # 32 * 20: dolphin emoji
    32 * 20: "🐬",
    # 32 * 10: octopus emoji
    32 * 10: "🐙",
    # 32 * 5: fish emoji
    32 * 5: "🐟",
    # 32 * 2: crab emoji
    32 * 2: "🦀",
    # 32 * 1: fried shrimp emoji
    32 * 1: "🍤",
    # 5: snail emoji
    5: "🐌",
    # 1: microbe emoji
    1: "🦠",
}


def get_sea_creature_for_holdings(holdings: float) -> str:
    """
    Returns the sea creature for the given holdings.
    :param holdings: The holdings to get the sea creature for.
    :return: The sea creature for the given holdings.
    """
    # if the holdings are more than 2 times the highest sea creature,
    # return the highest sea creature with a multiplier next to it
    highest_possible_holdings = max(sea_creatures.keys())
    if holdings >= 2 * highest_possible_holdings:
        creature_count = max(int(holdings / highest_possible_holdings), 10)
        return sea_creatures[highest_possible_holdings] * creature_count
    return next(
        (
            sea_creature
            for holding_value, sea_creature in sea_creatures.items()
            if holdings >= holding_value
        ),
        "",
    )


async def get_holding_for_address(address: ChecksumAddress) -> float:
    if price_cache["block"] != (b := await w3.eth.get_block_number()):
        price_cache["rpl_price"] = solidity.to_float(
            await rp.call("rocketNetworkPrices.getRPLPrice")
        )
        price_cache["reth_price"] = solidity.to_float(
            await rp.call("rocketTokenRETH.getExchangeRate")
        )
        price_cache["block"] = b

    # get their eth balance
    eth_balance: float = solidity.to_float(await w3.eth.get_balance(address))
    # get ERC-20 token balance for this address
    with contextlib.suppress(Exception):
        rpl_contract = await rp.get_contract_by_name("rocketTokenRPL")
        rplfs_contract = await rp.get_contract_by_name("rocketTokenRPLFixedSupply")
        reth_contract = await rp.get_contract_by_name("rocketTokenRETH")
        rpl_balance, rplfs_balance, reth_balance = await rp.multicall(
            [
                rpl_contract.functions.balanceOf(address),
                rplfs_contract.functions.balanceOf(address),
                reth_contract.functions.balanceOf(address),
            ]
        )
        eth_balance += solidity.to_float(rpl_balance) * price_cache["rpl_price"]
        eth_balance += solidity.to_float(rplfs_balance) * price_cache["rpl_price"]
        eth_balance += solidity.to_float(reth_balance) * price_cache["reth_price"]
    # add eth they provided for minipools
    eth_balance += solidity.to_float(
        await rp.call("rocketNodeStaking.getNodeETHBonded", address)
    )
    # add their staked RPL
    staked_rpl = solidity.to_float(
        await rp.call("rocketNodeStaking.getNodeStakedRPL", address)
    )
    eth_balance += staked_rpl * price_cache["rpl_price"]
    return eth_balance


async def get_sea_creature_for_address(address: ChecksumAddress) -> str:
    return get_sea_creature_for_holdings(await get_holding_for_address(address))
