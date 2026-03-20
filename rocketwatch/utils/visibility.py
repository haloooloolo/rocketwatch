from discord import Interaction

from plugins.support_utils.support_utils import has_perms


def is_hidden(interaction: Interaction):
    channel_name = getattr(interaction.channel, "name", None) or ""
    for allowed_channel in ["random", "rocket-watch", "trading"]:
        if allowed_channel in channel_name:
            return False
    return False


def is_hidden_role_controlled(interaction: Interaction):
    # reuses the has_perms function from support_utils, but overrides it when is_hidden would return false
    return not has_perms(interaction) if is_hidden(interaction) else False
