import logging
from datetime import datetime

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

log = logging.getLogger("rocketwatch.command_tree")


class RWCommandTree(CommandTree):
    async def _call(self, interaction: Interaction) -> None:
        cmd_name = interaction.command.name if interaction.command else "unknown"
        timestamp = datetime.utcnow()

        log.info(
            f"/{cmd_name} triggered by {interaction.user} in #{interaction.channel.name} ({interaction.guild})"
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
                        "name": interaction.channel.name,
                    },
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
                f"/{cmd_name} called by {interaction.user} in #{interaction.channel.name} ({interaction.guild}) failed"
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
            f" #{interaction.channel.name} ({interaction.guild}) completed successfully"
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

    async def on_error(self, interaction: Interaction, error: AppCommandError) -> None:
        cmd_name = interaction.command.name if interaction.command else "unknown"
        log.error(
            f"/{cmd_name} called by {interaction.user} in #{interaction.channel.name} ({interaction.guild}) failed"
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

        await self.client.on_app_command_error(interaction, error)
