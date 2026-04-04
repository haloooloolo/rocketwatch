import logging
import random
import time
from datetime import UTC
from typing import cast

from discord import Interaction
from discord.abc import Messageable
from discord.app_commands import command, guilds
from discord.ext.commands import Cog, is_owner
from eth_typing import HexStr
from web3.types import EventData, LogReceipt

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.file import TextFile
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.shared_w3 import w3

log = logging.getLogger("rocketwatch.debug")


class Debug(Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def raise_exception(self, interaction: Interaction) -> None:
        """
        Raise an exception for testing purposes.
        """
        with open(str(random.random()), "rb"):
            raise Exception("this should never happen wtf is your filesystem")

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def get_members_of_role(
        self, interaction: Interaction, guild_id: str, role_id: str
    ) -> None:
        """Get members of a role"""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = await self.bot.get_or_fetch_guild(int(guild_id))
            role = await self.bot.get_or_fetch_role(int(guild_id), int(role_id))
            # print name + identifier and id of each member
            members = [
                f"{member.name}#{member.discriminator}, ({member.id})"
                for member in role.members
            ]
            # generate a file with a header that mentions what role and guild the members are from
            content = (
                f"Members of {role.name} ({role.id}) in {guild.name} ({guild.id})\n\n"
                + "\n".join(members)
            )
            file = TextFile(content, "members.txt")
            await interaction.followup.send(file=file)
        except Exception as err:
            await interaction.followup.send(content=f"```{err!r}```")

    # list all roles of a guild with name and id
    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def get_roles(self, interaction: Interaction, guild_id: str) -> None:
        """Get roles of a guild"""
        await interaction.response.defer(ephemeral=True)
        try:
            guild = self.bot.get_guild(int(guild_id))
            assert guild is not None
            log.debug(guild)
            # print name + identifier and id of each member
            roles = [f"{role.name}, ({role.id})" for role in guild.roles]
            # generate a file with a header that mentions what role and guild the members are from
            content = f"Roles of {guild.name} ({guild.id})\n\n" + "\n".join(roles)
            file = TextFile(content, "roles.txt")
            await interaction.followup.send(file=file)
        except Exception as err:
            await interaction.followup.send(content=f"```{err!r}```")

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def delete_msg(self, interaction: Interaction, message_url: str) -> None:
        """
        Guess what. It deletes a message.
        """
        await interaction.response.defer(ephemeral=True)
        channel_id, message_id = message_url.split("/")[-2:]
        channel = await self.bot.get_or_fetch_channel(int(channel_id))
        assert isinstance(channel, Messageable)
        msg = await channel.fetch_message(int(message_id))
        await msg.delete()
        await interaction.followup.send(content="Done")

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def edit_embed(
        self, interaction: Interaction, message_url: str, new_description: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        channel_id, message_id = message_url.split("/")[-2:]
        channel = await self.bot.get_or_fetch_channel(int(channel_id))
        assert isinstance(channel, Messageable)
        msg = await channel.fetch_message(int(message_id))
        embed = msg.embeds[0]
        embed.description = new_description
        await msg.edit(embed=embed)
        await interaction.followup.send(content="Done")

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def debug_transaction(self, interaction: Interaction, tnx_hash: str) -> None:
        """
        Try to return the revert reason of a transaction.
        """
        await interaction.response.defer(ephemeral=True)
        transaction_receipt = await w3.eth.get_transaction(HexStr(tnx_hash))
        if revert_reason := await rp.get_revert_reason(transaction_receipt):
            await interaction.followup.send(
                content=f"```Revert reason: {revert_reason}```"
            )
        else:
            await interaction.followup.send(content="```No revert reason Available```")

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def purge_minipools(
        self, interaction: Interaction, confirm: bool = False
    ) -> None:
        """
        Purge minipools collection, so it can be resynced from scratch in the next update.
        """
        await interaction.response.defer(ephemeral=True)
        if not confirm:
            await interaction.followup.send(
                "Not running. Set `confirm` to `true` to run."
            )
            return
        await self.bot.db.minipools.drop()
        await interaction.followup.send(content="Done")

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def sync_commands(self, interaction: Interaction) -> None:
        """
        Full sync of the command tree
        """
        await interaction.response.defer(ephemeral=True)
        await self.bot.sync_commands()
        await interaction.followup.send(content="Done")

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def talk(
        self, interaction: Interaction, channel_id: str, message: str
    ) -> None:
        """
        Send a message to a channel.
        """
        await interaction.response.defer(ephemeral=True)
        channel = await self.bot.get_or_fetch_channel(int(channel_id))
        assert isinstance(channel, Messageable)
        await channel.send(message)
        await interaction.followup.send(content="Done")

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def announce(
        self, interaction: Interaction, channel_id: str, message: str
    ) -> None:
        """
        Send a message to a channel.
        """
        await interaction.response.defer(ephemeral=True)
        channel = await self.bot.get_or_fetch_channel(int(channel_id))
        assert isinstance(channel, Messageable)
        e = Embed(title="Announcement", description=message)
        e.add_field(
            name="Timestamp",
            value=f"<t:{int(time.time())}:R> (<t:{int(time.time())}:f>)",
        )
        await channel.send(embed=e)
        await interaction.followup.send(content="Done")

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def restore_support_template(
        self, interaction: Interaction, template_name: str, message_url: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        channel_id, message_id = message_url.split("/")[-2:]
        channel = await self.bot.get_or_fetch_channel(int(channel_id))
        assert isinstance(channel, Messageable)

        msg = await channel.fetch_message(int(message_id))
        template_embed = msg.embeds[0]
        template_title = template_embed.title
        assert template_embed.description is not None
        template_description = "\n".join(template_embed.description.splitlines()[:-2])

        import re
        from datetime import datetime

        edit_line = template_embed.description.splitlines()[-1]
        match = re.search(
            r"Last Edited by <@(?P<user>[0-9]+)> <t:(?P<ts>[0-9]+):R>", edit_line
        )
        if match is None:
            await interaction.followup.send(
                "Failed to restore support template. The provided message doesn't match the expected format."
            )
            return

        user_id = int(match.group("user"))
        ts = int(match.group("ts"))

        user = await self.bot.get_or_fetch_user(user_id)

        await self.bot.db.support_bot_dumps.insert_one(
            {
                "ts": datetime.fromtimestamp(ts, tz=UTC),
                "template": template_name,
                "prev": None,
                "new": {"title": template_title, "description": template_description},
                "author": {"id": user.id, "name": user.name},
            }
        )
        await self.bot.db.support_bot.insert_one(
            {
                "_id": template_name,
                "title": template_title,
                "description": template_description,
            }
        )

        await interaction.followup.send(content="Done")

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def restore_missed_events(
        self, interaction: Interaction, tx_hash: str
    ) -> None:
        import pickle
        from datetime import datetime

        from rocketwatch.plugins.log_events.log_events import LogEvents

        await interaction.response.defer(ephemeral=True)

        events_plugin = cast(LogEvents, self.bot.cogs["Events"])

        filtered_events = []
        for event_log in (await w3.eth.get_transaction_receipt(HexStr(tx_hash)))[
            "logs"
        ]:
            if ("topics" in event_log) and (
                event_log["topics"][0].hex() in events_plugin._topic_map
            ):
                filtered_events.append(event_log)

        channels = cfg.discord.channels
        events, _ = await events_plugin.process_events(filtered_events)
        for event in events:
            channel_candidates = [
                value
                for key, value in channels.items()
                if event.event_name.startswith(key)
            ]
            channel_id = (
                channel_candidates[0] if channel_candidates else channels["default"]
            )
            await self.bot.db.event_queue.insert_one(
                {
                    "_id": event.unique_id,
                    "embed": pickle.dumps(event.embed),
                    "topic": event.topic,
                    "event_name": event.event_name,
                    "block_number": event.block_number,
                    "score": event.get_score(),
                    "time_seen": datetime.now(),
                    "image": pickle.dumps(event.image) if event.image else None,
                    "thumbnail": pickle.dumps(event.thumbnail)
                    if event.thumbnail
                    else None,
                    "channel_id": channel_id,
                    "message_id": None,
                }
            )
            await interaction.followup.send(embed=event.embed)
        await interaction.followup.send(content="Done")


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(Debug(bot))
