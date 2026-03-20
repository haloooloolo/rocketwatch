import logging
import traceback
from pathlib import Path

from discord import Guild, Intents, Interaction, Role, Thread, User
from discord.abc import GuildChannel, Messageable, PrivateChannel
from discord.ext.commands import Bot
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from utils.command_tree import RWCommandTree
from utils.config import cfg
from utils.file import TextFile
from utils.retry import retry
from utils.rocketpool import rp

log = logging.getLogger("rocketwatch.bot")


class RocketWatch(Bot):
    def __init__(self, intents: Intents) -> None:
        super().__init__(command_prefix=(), tree_cls=RWCommandTree, intents=intents)
        self.db: AsyncDatabase = AsyncMongoClient(cfg.mongodb.uri).rocketwatch

    async def _load_plugins(self):
        chain = cfg.rocketpool.chain
        storage = cfg.rocketpool.manual_addresses["rocketStorage"]
        log.info(f"Running using storage contract {storage} (Chain: {chain})")

        log.info("Loading plugins...")
        included_modules = set(cfg.modules.include or [])
        excluded_modules = set(cfg.modules.exclude or [])

        def should_load_plugin(_plugin: str) -> bool:
            # inclusion takes precedence in case of collision
            if _plugin in included_modules:
                log.debug(f"Plugin {_plugin} explicitly included")
                return True
            elif _plugin in excluded_modules:
                log.debug(f"Plugin {_plugin} explicitly excluded")
                return False
            elif len(included_modules) > 0:
                log.debug(f"Plugin {_plugin} implicitly excluded")
                return False
            else:
                log.debug(f"Plugin {_plugin} implicitly included")
                return True

        for path in Path("plugins").glob("**/*.py"):
            plugin_name = path.stem
            if not should_load_plugin(plugin_name):
                log.warning(f"Skipping plugin {plugin_name}")
                continue

            log.info(f'Loading plugin "{plugin_name}"')
            try:
                extension_name = f"plugins.{plugin_name}.{plugin_name}"
                await self.load_extension(extension_name)
            except Exception as e:
                log.exception(f'Failed to load plugin "{plugin_name}"')
                await self.report_error(e)

        log.info("Finished loading plugins")

    async def setup_hook(self) -> None:
        await rp.async_init()
        await self._load_plugins()

    async def sync_commands(self) -> None:
        log.info("Syncing command tree...")
        await self.tree.sync()
        for guild in self.guilds:
            await self.tree.sync(guild=guild)

    def clear_commands(self) -> None:
        self.tree.clear_commands(guild=None)
        for guild in self.guilds:
            self.tree.clear_commands(guild=guild)

    async def on_ready(self):
        assert self.user is not None
        log.info(f"Logged in as {self.user.name} ({self.user.id})")
        commands_enabled = cfg.modules.enable_commands
        if not commands_enabled:
            log.info("Commands disabled, clearing local tree...")
            self.clear_commands()
            if commands_enabled is None:
                log.info("Sync behavior unspecified, skipping")
                return

        await self.sync_commands()

    async def get_or_fetch_guild(self, guild_id: int) -> Guild:
        return self.get_guild(guild_id) or await self.fetch_guild(guild_id)

    async def get_or_fetch_channel(
        self, channel_id: int
    ) -> GuildChannel | PrivateChannel | Thread:
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    async def get_or_fetch_user(self, user_id: int) -> User:
        return self.get_user(user_id) or await self.fetch_user(user_id)

    async def get_or_fetch_role(self, guild_id: int, role_id: int) -> Role:
        guild = await self.get_or_fetch_guild(guild_id)
        return guild.get_role(role_id) or await guild.fetch_role(role_id)

    async def report_error(
        self, exception: Exception, interaction: Interaction | None = None, *args
    ) -> None:
        err_description = f"`{repr(exception)[:150]}`"

        if args:
            args_fmt = "\n".join(f"args[{i}] = {arg}" for i, arg in enumerate(args))
            err_description += f"\n```{args_fmt}```"

        if interaction:
            if interaction.command:
                cmd_name = interaction.command.name
            else:
                cmd_name = getattr(interaction, "data", {}).get("name", "unknown")
            cmd_options = (
                interaction.namespace.__dict__
                if interaction.namespace
                else (interaction.data.get("options", []) if interaction.data else [])
            )
            err_description += (
                f"\n```"
                f"command = {cmd_name}\n"
                f"command.params = {cmd_options}\n"
                f"channel = {interaction.channel}\n"
                f"user = {interaction.user}"
                f"```"
            )

        error = getattr(exception, "original", exception)
        err_trace = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        log.error(err_trace)

        try:
            channel = await self.get_or_fetch_channel(cfg.discord.channels["errors"])
            file = TextFile(err_trace, "exception.txt")
            assert isinstance(channel, Messageable), (
                f"Error channel {channel} is not messageable"
            )
            await retry(tries=5, delay=5)(channel.send)(err_description, file=file)
        except Exception:
            log.exception("Failed to send message.")
