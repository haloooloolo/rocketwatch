import logging
from datetime import datetime
from typing import TYPE_CHECKING

from discord import Interaction
from discord.app_commands import AppCommandError, CommandTree
from discord.app_commands.errors import (
    BotMissingPermissions,
    CheckFailure,
    CommandOnCooldown,
    MissingPermissions,
    NoPrivateMessage,
    TransformerError,
)

from utils.config import cfg

if TYPE_CHECKING:
    from rocketwatch.rocketwatch import RocketWatch

log = logging.getLogger("rocketwatch.command_tree")


def _channel_name(interaction: Interaction) -> str:
    return getattr(interaction.channel, "name", None) or "DM"


class RWCommandTree(CommandTree["RocketWatch"]):
    async def _call(self, interaction: Interaction["RocketWatch"]) -> None:
        if not cfg.modules.enable_commands:
            return

        cmd_name = interaction.command.name if interaction.command else "unknown"
        timestamp = datetime.utcnow()

        channel_name = _channel_name(interaction)

        log.info(
            f"/{cmd_name} triggered by {interaction.user} in #{channel_name} ({interaction.guild})"
        )
        try:
            await self.client.db.command_metrics.insert_one(
                {
                    "_id": interaction.id,
                    "command": cmd_name,
                    "options": interaction.data.get("options", [])
                    if interaction.data
                    else [],
                    "user": {
                        "id": interaction.user.id,
                        "name": interaction.user.name,
                    },
                    "guild": {
                        "id": interaction.guild.id,
                        "name": interaction.guild.name,
                    }
                    if interaction.guild
                    else None,
                    "channel": {
                        "id": interaction.channel.id,
                        "name": channel_name,
                    }
                    if interaction.channel
                    else None,
                    "timestamp": timestamp,
                    "status": "pending",
                }
            )
        except Exception as e:
            log.error(f"Failed to insert command into database: {e}")
            await self.client.report_error(e)

        try:
            await super()._call(interaction)
        except Exception as error:
            log.info(
                f"/{cmd_name} called by {interaction.user} in #{channel_name} ({interaction.guild}) failed"
            )
            try:
                await self.client.db.command_metrics.update_one(
                    {"_id": interaction.id},
                    {
                        "$set": {
                            "status": "error",
                            "took": (datetime.utcnow() - timestamp).total_seconds(),
                            "error": str(error),
                        }
                    },
                )
            except Exception as e:
                log.exception("Failed to update command status to error")
                await self.client.report_error(e)
            raise

        log.info(
            f"/{cmd_name} called by {interaction.user} in"
            f" #{channel_name} ({interaction.guild}) completed successfully"
        )
        try:
            await self.client.db.command_metrics.update_one(
                {"_id": interaction.id},
                {
                    "$set": {
                        "status": "completed",
                        "took": (datetime.utcnow() - timestamp).total_seconds(),
                    }
                },
            )
        except Exception as e:
            log.error(f"Failed to update command status to completed: {e}")
            await self.client.report_error(e)

    async def on_error(
        self, interaction: Interaction["RocketWatch"], error: AppCommandError
    ) -> None:
        cmd_name = interaction.command.name if interaction.command else "unknown"
        channel_name = _channel_name(interaction)
        log.error(
            f"/{cmd_name} called by {interaction.user} in #{channel_name} ({interaction.guild}) failed"
        )

        if isinstance(error, CommandOnCooldown):
            msg = f"Slow down! You are using this command too fast. Please try again in {error.retry_after:.0f} seconds."
        elif isinstance(error, MissingPermissions):
            msg = f"You don't have the required permissions to use this command. Missing: {', '.join(error.missing_permissions)}"
        elif isinstance(error, BotMissingPermissions):
            msg = f"I'm missing the required permissions to run this command. Missing: {', '.join(error.missing_permissions)}"
        elif isinstance(error, NoPrivateMessage):
            msg = "This command can only be used in a server, not in DMs."
        elif isinstance(error, CheckFailure):
            msg = "You don't meet the requirements to use this command."
        elif isinstance(error, TransformerError):
            msg = f"Failed to process the value for `{error.value}`. Please check your input and try again."
        else:
            msg = "An unexpected error occurred and has been reported to the developer. Please try again later."

        try:
            await self.client.report_error(error, interaction)
            await interaction.followup.send(content=msg, ephemeral=True)
        except Exception:
            log.exception("Failed to alert user")
