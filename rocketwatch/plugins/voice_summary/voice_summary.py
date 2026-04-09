from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

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
from rocketwatch.plugins.voice_summary.session import CallSession
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

    def _count_voice_users(self, channel: VocalGuildChannel) -> int:
        """Count non-bot members in a voice channel."""
        return sum(1 for m in channel.members if not m.bot)

    @Cog.listener()
    async def on_voice_state_update(
        self,
        member: Member,
        before: VoiceState,
        after: VoiceState,
    ) -> None:
        if member.bot:
            return

        # left old channel
        if before.channel is not None:
            count = self._count_voice_users(before.channel)
            if count == 0:
                await self._stop_and_process()
            else:
                self._start_grace()

        # joined new channel
        if after.channel is not None:
            count = self._count_voice_users(after.channel)
            if count >= self._config.min_users:
                self._cancel_grace()
                if not self._session:
                    await self._start_recording(after.channel)

    def _start_grace(self) -> None:
        """Start the grace period before auto-disconnecting."""
        self._cancel_grace()
        self._grace_task = asyncio.create_task(self._grace_countdown())

    def _cancel_grace(self) -> None:
        if self._grace_task and not self._grace_task.done():
            self._grace_task.cancel()
            self._grace_task = None

    async def _grace_countdown(self) -> None:
        try:
            await asyncio.sleep(self._config.leave_grace_seconds)
            await self._stop_and_process()
        except asyncio.CancelledError:
            pass

    async def _start_recording(self, channel: VocalGuildChannel) -> None:
        log.info(f"Auto-joining voice channel: {channel.name}")
        self._session = CallSession(self._pipeline)
        try:
            await self._session.start(channel)
        except Exception:
            log.exception("Failed to connect to voice channel")
            self._session = None
            return

        self._timeout_task = asyncio.create_task(self._recording_timeout())

    async def _recording_timeout(self) -> None:
        try:
            await asyncio.sleep(180 * 60)
            log.warning("Max recording duration reached, stopping")
            await self._stop_and_process()
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

    async def _resolve_usernames(self, user_ids: set[int]) -> dict[int, str]:
        """Resolve user IDs to display names."""
        usernames: dict[int, str] = {}
        guild_id = cfg.discord.owner.server_id
        for user_id in user_ids:
            if member := await self.bot.get_or_fetch_member(guild_id, user_id):
                usernames[user_id] = member.display_name
            else:
                usernames[user_id] = str(user_id)
        return usernames

    @staticmethod
    def _mentionify(text: str, usernames: dict[int, str]) -> str:
        """Replace display names with Discord mentions."""
        for user_id, name in sorted(
            usernames.items(), key=lambda x: len(x[1]), reverse=True
        ):
            text = re.sub(re.escape(name), f"<@{user_id}>", text, flags=re.IGNORECASE)
        return text

    async def _stop_and_process(self) -> None:
        self._cancel_scheduled_tasks()

        session = self._session
        self._session = None
        if not session:
            return

        recorder = await session.stop()
        if not recorder:
            return

        if recorder.speaker_count == 0:
            log.info("No speakers detected, discarding recording")
            session.cleanup()
            return

        try:
            await session.await_pending_transcriptions()

            user_segments = recorder.get_user_segments()
            if not user_segments:
                log.info("Empty recording, discarding")
                return

            session.register_final_segments(user_segments)
            await session.transcribe_remaining()

            all_segments = session.collect_segments()
            usernames = await self._resolve_usernames(set(user_segments))

            transcript = TranscriptionPipeline.format_transcript(
                all_segments, usernames
            )
            session.save_transcript(transcript)

            summary = await self._pipeline.summarize(transcript)
            audio = await asyncio.to_thread(session.mix_audio, user_segments)

            if not summary:
                log.info("No substantive content, discarding")
                return

            summary = self._mentionify(summary, usernames)
            await self._post_results(transcript, summary, audio)
        except Exception as e:
            await self.bot.report_error(e)
        finally:
            session.cleanup()

    async def _post_results(
        self, transcript: str, summary: str, audio_path: Path
    ) -> None:
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
                ui.TextDisplay(summary[:4000]),
                ui.Separator(),
                ui.TextDisplay("-# Attachments"),
                ui.File("attachment://recording.mp3"),
                ui.File("attachment://transcript.txt"),
                accent_color=ACCENT_COLOR,
            )
        )

        files = [
            File(audio_path, filename="recording.mp3"),
            TextFile(transcript, "transcript.txt"),
        ]
        await channel.send(
            view=view, files=files, allowed_mentions=AllowedMentions.none()
        )
        log.info("Transcript and summary posted")

    async def cog_unload(self) -> None:
        self._cancel_scheduled_tasks()
        if self._session:
            await self._session.stop()
            self._session.cleanup()
            self._session = None


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(VoiceSummary(bot))
