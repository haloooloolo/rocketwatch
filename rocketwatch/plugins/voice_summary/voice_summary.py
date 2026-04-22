from __future__ import annotations

import asyncio
import logging
from typing import Any

from discord import (
    AllowedMentions,
    File,
    Interaction,
    Member,
    VoiceState,
    ui,
)
from discord.abc import Messageable
from discord.app_commands import command
from discord.channel import StageChannel, VocalGuildChannel, VoiceChannel
from discord.ext.commands import Cog, is_owner
from discord.ext.voice_recv import VoiceRecvClient

from rocketwatch.bot import RocketWatch
from rocketwatch.plugins.voice_summary.pipeline import TranscriptionPipeline
from rocketwatch.plugins.voice_summary.session import (
    CallResult,
    CallSession,
)
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import ACCENT_COLOR
from rocketwatch.utils.file import TextFile
from rocketwatch.utils.llm import create_provider

log = logging.getLogger("rocketwatch.voice_summary")


class VoiceSummary(Cog):
    def __init__(self, bot: RocketWatch) -> None:
        self.bot = bot
        self._config = cfg.transcription
        self._session: CallSession | None = None
        self._starting = False
        self._stopping = False
        self._resume_task: asyncio.Task[None] | None = None
        self._grace_task: asyncio.Task[None] | None = None
        self._timeout_task: asyncio.Task[None] | None = None

        llm = create_provider(self._config.llm)
        assert llm and self._config.stt.provider
        self._pipeline = TranscriptionPipeline(
            stt_config=self._config.stt,
            llm_provider=llm,
        )

    @staticmethod
    def _count_voice_users(channel: VocalGuildChannel) -> int:
        """Count non-bot members in a voice channel."""
        return sum(1 for m in channel.members if not m.bot)

    def _get_recorded_channel(self) -> VocalGuildChannel | None:
        if self._session and self._session.voice_client:
            return self._session.voice_client.channel
        return None

    async def _handle_bot_voice_state_change(
        self, before: VoiceState, after: VoiceState
    ) -> None:
        if before.channel and (before.channel != after.channel) and self._session:
            if (after.channel is None) and (not self._stopping):
                # Try to rejoin and resume the existing session
                self._resume_task = asyncio.create_task(
                    self._resume_session(before.channel)
                )
                return

            await self._stop_recording()

        if after.channel and (not self._session):
            await self._start_recording(after.channel)

    async def _handle_member_voice_state_change(
        self, before: VoiceState, after: VoiceState
    ) -> None:
        recorded_channel = self._get_recorded_channel()

        # left recorded channel
        if before.channel and (before.channel == recorded_channel):
            count = self._count_voice_users(before.channel)
            if count == 0:
                await self._disconnect_voice()
            else:
                self._start_grace()

        # joined new channel
        if after.channel is not None:
            count = self._count_voice_users(after.channel)
            if count >= self._config.min_users:
                if after.channel == recorded_channel:
                    self._cancel_grace()
                elif not self._session:
                    await self._connect_voice(after.channel)

    @Cog.listener()
    async def on_voice_state_update(
        self,
        member: Member,
        before: VoiceState,
        after: VoiceState,
    ) -> None:
        if member.guild.id != cfg.rocketpool.support.server_id:
            return

        if self.bot.user and (member.id == self.bot.user.id):
            # bot's own voice state changed
            await self._handle_bot_voice_state_change(before, after)
            return

        if member.bot:
            # we don't care about other bots
            return

        await self._handle_member_voice_state_change(before, after)

    def _start_grace(self) -> None:
        """Start the grace period before auto-disconnecting."""
        if self._grace_task and not self._grace_task.done():
            return
        self._grace_task = asyncio.create_task(
            self._delayed_stop(self._config.leave_grace_seconds)
        )

    def _cancel_grace(self) -> None:
        if self._grace_task and not self._grace_task.done():
            self._grace_task.cancel()
            self._grace_task = None

    async def _connect_voice(self, channel: VocalGuildChannel) -> None:
        """Connect to a voice channel. Session setup happens via voice state event."""
        if channel.guild.voice_client or self._starting:
            return
        try:
            await channel.connect(cls=VoiceRecvClient)
        except Exception:
            log.exception("Failed to connect to voice channel")

    async def _start_recording(self, channel: VocalGuildChannel) -> None:
        """Set up a recording session on a channel the bot is already in."""
        if self._starting:
            return
        self._starting = True
        try:
            vc = channel.guild.voice_client
            if not vc:
                log.warning("No voice client found, cannot start recording")
                return

            log.info(f"Starting recording in voice channel: {channel.name}")
            self._session = CallSession(self._pipeline, self.bot)
            assert isinstance(vc, VoiceRecvClient)
            await self._session.start(vc)
            self._timeout_task = asyncio.create_task(self._delayed_stop(180 * 60))
        finally:
            self._starting = False

    async def _disconnect_voice(self) -> None:
        """Disconnect from voice. Session cleanup happens via voice state event."""
        self._stopping = True
        self._cancel_scheduled_tasks()
        if self._session and self._session.voice_client:
            await self._session.voice_client.disconnect()
        elif self._session:
            # No active voice client (e.g. mid-resume) — finalize directly
            await self._stop_recording()

    async def _resume_session(self, channel: VocalGuildChannel) -> None:
        """Rejoin the given channel and re-attach recording to the existing session."""
        log.warning(f"Voice connection lost in {channel.name}, attempting to resume")
        for attempt in range(5):
            if self._stopping or not self._session:
                return
            try:
                vc = await channel.connect(cls=VoiceRecvClient)
                await self._session.resume(vc)
                log.info("Voice session resumed")
                return
            except Exception:
                log.exception(f"Resume attempt {attempt + 1} failed")
            await asyncio.sleep(2**attempt)

        log.error("Could not resume voice session, finalizing")
        await self._stop_recording()

    async def _delayed_stop(self, delay: float) -> None:
        """Wait, then disconnect from voice."""
        try:
            await asyncio.sleep(delay)
            await self._disconnect_voice()
        except asyncio.CancelledError:
            pass

    def _cancel_scheduled_tasks(self) -> None:
        """Cancel grace, timeout, and resume tasks, if running."""
        current = asyncio.current_task()
        for task in (self._grace_task, self._timeout_task, self._resume_task):
            if task and task is not current and not task.done():
                task.cancel()
        self._grace_task = None
        self._timeout_task = None
        self._resume_task = None

    async def _stop_recording(self) -> None:
        self._cancel_scheduled_tasks()

        session = self._session
        self._session = None
        self._stopping = False
        if not session:
            return

        try:
            result = await session.finalize()
            if not result:
                return

            await self._post_results(result)
        except Exception as e:
            await self.bot.report_error(e)

    async def _post_results(self, result: CallResult) -> None:
        channel_id = self._config.output_channel_id
        if not channel_id:
            log.warning("No output channel configured")
            return

        channel = await self.bot.get_or_fetch_channel(channel_id)
        assert isinstance(channel, Messageable)

        guild = getattr(channel, "guild", None)
        upload_limit = guild.filesize_limit if guild else (10 * 1024 * 1024)
        audio_size = result.audio_path.stat().st_size

        file_components: list[ui.File[Any]] = []
        files: list[File] = []

        if audio_size < upload_limit:
            file_components.append(ui.File("attachment://recording.mp3"))
            files.append(File(result.audio_path, filename="recording.mp3"))
        else:
            log.warning(
                f"Skipping audio attachment: {audio_size} bytes exceeds "
                f"upload limit of {upload_limit} bytes"
            )

        file_components.append(ui.File("attachment://transcript.txt"))
        files.append(TextFile(result.transcript, "transcript.txt"))

        header = "# Voice Call Summary"
        footer = "-# Attachments"
        # Discord caps total displayable text per message at 4000 chars
        max_body_size = 4000 - len(header) - len(footer)

        view = ui.LayoutView()
        view.add_item(
            ui.Container(
                ui.TextDisplay(header),
                ui.Separator(),
                ui.TextDisplay(result.summary[:max_body_size]),
                ui.Separator(),
                ui.TextDisplay(footer),
                *file_components,
                accent_color=ACCENT_COLOR,
            )
        )

        await channel.send(
            view=view, files=files, allowed_mentions=AllowedMentions.none()
        )
        log.info("Transcript and summary posted")

    @command()
    @is_owner()
    async def start_recording(
        self, interaction: Interaction, channel: VoiceChannel | StageChannel
    ) -> None:
        """Manually start recording in a voice channel."""
        await interaction.response.defer(ephemeral=True)

        if self._session:
            await interaction.followup.send("Already recording.")
            return

        await self._connect_voice(channel)
        await interaction.followup.send(f"Recording started in {channel.jump_url}.")

    @command()
    @is_owner()
    async def stop_recording(self, interaction: Interaction) -> None:
        """Manually stop voice recording and produce summary."""
        await interaction.response.defer(ephemeral=True)

        if not self._session:
            await interaction.followup.send("Not currently recording.")
            return

        await self._disconnect_voice()
        await interaction.followup.send("Recording stopped.")

    async def cog_unload(self) -> None:
        self._stopping = True
        self._cancel_scheduled_tasks()
        if self._session:
            await self._session.stop()
            self._session = None


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(VoiceSummary(bot))
