import logging
from datetime import datetime

from discord import Interaction
from discord.app_commands import CommandTree, AppCommandError

from utils.cfg import cfg

log = logging.getLogger("command_tree")
log.setLevel(cfg.log_level)


class RWCommandTree(CommandTree):
    async def _call(self, interaction: Interaction) -> None:
        cmd_name = interaction.command.name if interaction.command else "unknown"
        timestamp = datetime.utcnow()

        log.info(f"/{cmd_name} triggered by {interaction.user} in #{interaction.channel.name} ({interaction.guild})")
        try:
            await self.client.db.command_metrics.insert_one({
                '_id': interaction.id,
                'command': cmd_name,
                'options': interaction.data.get("options", []) if interaction.data else [],
                'user': {
                    'id': interaction.user.id,
                    'name': interaction.user.name,
                },
                'guild': {
                    'id': interaction.guild.id,
                    'name': interaction.guild.name,
                } if interaction.guild else None,
                'channel': {
                    'id': interaction.channel.id,
                    'name': interaction.channel.name,
                },
                'timestamp': timestamp,
                'status': 'pending'
            })
        except Exception as e:
            log.error(f"Failed to insert command into database: {e}")
            await self.client.report_error(e)

        try:
            await super()._call(interaction)
        except Exception as error:
            log.info(f"/{cmd_name} called by {interaction.user} in #{interaction.channel.name} ({interaction.guild}) failed")
            try:
                await self.client.db.command_metrics.update_one(
                    {'_id': interaction.id},
                    {'$set': {
                        'status': 'error',
                        'took': (datetime.utcnow() - timestamp).total_seconds(),
                        'error': str(error)
                    }}
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
                {'_id': interaction.id},
                {'$set': {
                    'status': 'completed',
                    'took': (datetime.utcnow() - timestamp).total_seconds()
                }}
            )
        except Exception as e:
            log.error(f"Failed to update command status to completed: {e}")
            await self.client.report_error(e)

    async def on_error(self, interaction: Interaction, error: AppCommandError) -> None:
        await self.client.on_app_command_error(interaction, error)
