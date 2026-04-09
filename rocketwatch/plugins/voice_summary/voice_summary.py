from __future__ import annotations

import asyncio
import logging

from discord import (
    AllowedMentions,
    File,
    Member,
    VoiceState,
    ui,
)
from discord.abc import Messageable
from discord.channel import VocalGuildChannel
from discord.ext.commands import Cog

from rocketwatch.bot import RocketWatch
from rocketwatch.plugins.voice_summary.pipeline import TranscriptionPipeline
from rocketwatch.plugins.voice_summary.session import CallResult, CallSession
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import ACCENT_COLOR
from rocketwatch.utils.file import TextFile
from rocketwatch.utils.llm import create_provider

log = logging.getLogger("rocketwatch.voice_summary")
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)


class VoiceSummary(Cog):
    def __init__(self, bot: RocketWatch) -> None:
        self.bot = bot
        self._config = cfg.transcription
        self._session: CallSession | None = None
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

    @Cog.listener()
    async def on_voice_state_update(
        self,
        member: Member,
        before: VoiceState,
        after: VoiceState,
    ) -> None:
        if member.bot:
            return

        recorded_channel = self._get_recorded_channel()

        # left recorded channel
        if before.channel and (before.channel == recorded_channel):
            count = self._count_voice_users(before.channel)
            if count == 0:
                await self._stop_recording()
            else:
                self._start_grace()

        # joined new channel
        if after.channel is not None:
            count = self._count_voice_users(after.channel)
            if count >= self._config.min_users:
                if after.channel == recorded_channel:
                    self._cancel_grace()
                elif not self._session:
                    await self._start_recording(after.channel)

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

    async def _start_recording(self, channel: VocalGuildChannel) -> None:
        log.info(f"Auto-joining voice channel: {channel.name}")
        self._session = CallSession(self._pipeline, self.bot)
        try:
            await self._session.start(channel)
        except Exception:
            log.exception("Failed to connect to voice channel")
            self._session = None
            return

        self._timeout_task = asyncio.create_task(self._delayed_stop(180 * 60))

    async def _delayed_stop(self, delay: float) -> None:
        """Wait, then stop and process the current session."""
        try:
            await asyncio.sleep(delay)
            await self._stop_recording()
        except asyncio.CancelledError:
            pass

    def _cancel_scheduled_tasks(self) -> None:
        """Cancel grace and timeout tasks, if running."""
        current = asyncio.current_task()
        for task in (self._grace_task, self._timeout_task):
            if task and task is not current and not task.done():
                task.cancel()
        self._grace_task = None
        self._timeout_task = None

    async def _stop_recording(self) -> None:
        self._cancel_scheduled_tasks()

        session = self._session
        self._session = None
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

        view = ui.LayoutView()
        view.add_item(
            ui.Container(
                ui.TextDisplay("# Voice Call Summary"),
                ui.Separator(),
                ui.TextDisplay(result.summary[:4000]),
                ui.Separator(),
                ui.TextDisplay("-# Attachments"),
                ui.File("attachment://recording.mp3"),
                ui.File("attachment://transcript.txt"),
                accent_color=ACCENT_COLOR,
            )
        )

        files = [
            File(result.audio_path, filename="recording.mp3"),
            TextFile(result.transcript, "transcript.txt"),
        ]
        await channel.send(
            view=view, files=files, allowed_mentions=AllowedMentions.none()
        )
        log.info("Transcript and summary posted")

    async def cog_unload(self) -> None:
        self._cancel_scheduled_tasks()
        if self._session:
            await self._session.stop()
            self._session = None


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(VoiceSummary(bot))
