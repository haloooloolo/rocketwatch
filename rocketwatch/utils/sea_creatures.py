sea_creatures = {
    # 32 * 60: spouting whale emoji
    32 * 60: '🐳',
    # 32 * 30: whale emoji
    32 * 30: '🐋',
    # 32 * 15: shark emoji
    32 * 15: '🦈',
    # 32 * 10: dolphin emoji
    32 * 10: '🐬',
    # 32 * 5: octopus emoji
    32 * 5: '🐙',
    # 32 * 2: fish emoji
    32 * 2: '🐟',
    # 32 * 1: fired shrimp emoji
    32 * 1: '🍤',
}


def get_sea_creature_for_holdings(holdings):
    """
    Returns the sea creature for the given holdings.
    :param holdings: The holdings to get the sea creature for.
    :return: The sea creature for the given holdings.
    """
    for holding_value, sea_creature in sea_creatures.items():
        if holdings >= holding_value:
            return sea_creature
    return ''