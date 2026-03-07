import logging
from datetime import datetime, timedelta

from discord import Interaction
from discord.app_commands import command, guilds
from discord.ext import commands, tasks
from discord.ext.commands import is_owner

from rocketwatch import RocketWatch
from utils.cfg import cfg
from utils.embeds import Embed

log = logging.getLogger("rich_activity")
log.setLevel(cfg.log_level)


class PinnedMessages(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

        if not self.run_loop.is_running() and bot.is_ready():
            self.run_loop.start()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.run_loop.is_running():
            return
        self.run_loop.start()

    @tasks.loop(seconds=60.0)
    async def run_loop(self):
        # get all pinned messages in db
        messages = await self.bot.db.pinned_messages.find().to_list()
        for message in messages:
            # if it's older than 6 hours and not disabled, mark as disabled
            if message["created_at"] + timedelta(hours=6) < datetime.utcnow() and not message["disabled"]:
                await self.bot.db.pinned_messages.update_one({"_id": message["_id"]}, {"$set": {"disabled": True}})
                message["disabled"] = True
            try:
                # check if it's marked as disabled but not cleaned_up
                if message["disabled"] and not message["cleaned_up"]:
                    # get channel
                    channel = self.bot.get_channel(message["channel_id"])
                    # get message
                    msg = await channel.fetch_message(message["message_id"])
                    # delete message
                    await msg.delete()
                    # mark as cleaned_up
                    await self.bot.db.pinned_messages.update_one({"_id": message["_id"]}, {"$set": {"cleaned_up": True}})
                elif not message["disabled"]:
                    # delete and resend message
                    channel = self.bot.get_channel(message["channel_id"])
                    # check if we have message sent already and if its the latest message in the channel
                    if message.get("message_id"):
                        messages = [message async for message in channel.history(limit=5)]
                        # if it isnt within the last 5 messages, we need to resend it
                        if any(m.id == message["message_id"] for m in messages):
                            continue
                        msg = await channel.fetch_message(message["message_id"])
                        await msg.delete()
                    e = Embed()
                    e.title = message["title"]
                    e.description = message["content"]
                    e.set_footer(
                        text=(
                            "This message has been pinned by Invis."
                            " Will be automatically removed if not updated within 6 hours."
                        ))
                    m = await channel.send(embed=e)
                    await self.bot.db.pinned_messages.update_one({"_id": message["_id"]}, {"$set": {"message_id": m.id}})
            except Exception as err:
                await self.bot.report_error(err)

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def pin(self, interaction: Interaction, channel_id: int, title: str, description: str):
        await interaction.response.defer()
        # check if channel exists
        channel = self.bot.get_channel(channel_id)
        if not channel:
            await interaction.followup.send("Channel not found")
            return
        # check if we already have a pinned message
        message = await self.bot.db.pinned_messages.find_one({"channel_id": channel.id})
        if message:
            # update message
            await self.bot.db.pinned_messages.update_one({"_id": message["_id"]}, {
                "$set": {"title"     : title, "content": description, "disabled": False, "cleaned_up": False,
                         "message_id": None, "created_at": datetime.utcnow()}})
            # rest is done by the run_loop
            await interaction.followup.send("Updated pinned message")
            return
        # create new message
        await self.bot.db.pinned_messages.insert_one(
            {"channel_id": channel.id, "message_id": None, "title": title, "content": description, "disabled": False,
             "cleaned_up": False, "created_at": datetime.utcnow()})
        # rest is done by the run_loop
        await interaction.followup.send("Created pinned message")

    @command()
    @guilds(cfg.discord.owner.server_id)
    @is_owner()
    async def unpin(self, interaction: Interaction, channel_id: str):
        await interaction.response.defer()
        # check if channel exists
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            await interaction.followup.send("Channel not found")
            return
        # check if we already have a pinned message
        message = await self.bot.db.pinned_messages.find_one({"channel_id": channel.id})
        if not message:
            await interaction.followup.send("No pinned message found")
            return
        # check if its already marked as disabled
        if message["disabled"]:
            await interaction.followup.send("Pinned message already disabled")
            return
        # soft delete
        await self.bot.db.pinned_messages.update_one({"_id": message["_id"]}, {"$set": {"disabled": True}})
        # rest is done by the run_loop
        await interaction.followup.send("Disabled pinned message")

    async def cog_unload(self):
        self.run_loop.cancel()


async def setup(bot):
    await bot.add_cog(PinnedMessages(bot))
