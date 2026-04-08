from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import davey
from discord import File, Member, VoiceChannel, VoiceClient, VoiceState
from discord.abc import Messageable
from discord.ext.commands import Cog
from discord.ext.voice_recv import BasicSink, VoiceRecvClient
from pydub import AudioSegment

from rocketwatch.bot import RocketWatch

if TYPE_CHECKING:
    from discord import User
    from discord.ext.voice_recv.opus import VoiceData

from rocketwatch.plugins.voice_summary.pipeline import TranscriptionPipeline
from rocketwatch.plugins.voice_summary.recorder import CallRecorder
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.file import TextFile
from rocketwatch.utils.llm import create_provider

TRANSCRIPTIONS_DIR = Path("../voice_calls")

log = logging.getLogger("rocketwatch.voice_summary")
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)


class VoiceSummary(Cog):
    def __init__(self, bot: RocketWatch) -> None:
        self.bot = bot
        self._config = cfg.transcription
        self._recorder: CallRecorder | None = None
        self._voice_client: VoiceClient | None = None
        self._artifact_dir: Path | None = None
        self._grace_task: asyncio.Task[None] | None = None
        self._timeout_task: asyncio.Task[None] | None = None
        self._pending_futures: list[Future[None]] = []

        llm = create_provider(self._config.llm)
        if llm and self._config.stt.provider:
            self._pipeline: TranscriptionPipeline | None = TranscriptionPipeline(
                stt_config=self._config.stt,
                llm_provider=llm,
            )
        else:
            self._pipeline = None
            log.warning("Transcription plugin loaded but LLM/STT not configured")

    def _count_voice_users(self, channel: VoiceChannel) -> int:
        """Count non-bot members in a voice channel."""
        return sum(1 for m in channel.members if not m.bot)

    @Cog.listener()
    async def on_voice_state_update(
        self,
        member: Member,
        before: VoiceState,
        after: VoiceState,
    ) -> None:
        if member.bot or not self._config.voice_channel_id or not self._pipeline:
            return

        target_id = self._config.voice_channel_id

        # Someone joined the target channel
        if after.channel and after.channel.id == target_id:
            assert isinstance(after.channel, VoiceChannel)
            self._cancel_grace()
            count = self._count_voice_users(after.channel)
            if count >= self._config.min_users and not self._recorder:
                await self._start_recording(after.channel)

        # Someone left the target channel
        if before.channel and before.channel.id == target_id:
            assert isinstance(before.channel, VoiceChannel)
            count = self._count_voice_users(before.channel)
            if count == 0 and self._recorder:
                await self._stop_and_process()
            elif count < self._config.min_users and self._recorder:
                self._start_grace()

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

    async def _start_recording(self, channel: VoiceChannel) -> None:
        log.info(f"Auto-joining voice channel: {channel.name}")
        try:
            vc = await channel.connect(cls=VoiceRecvClient)
        except Exception:
            log.exception("Failed to connect to voice channel")
            return

        self._voice_client = vc
        loop = asyncio.get_running_loop()

        def on_segment_closed(user_id: int, offset: float, wav_path: Path) -> None:
            future = asyncio.run_coroutine_threadsafe(
                self._transcribe_segment(user_id, offset, wav_path), loop
            )
            self._pending_futures.append(future)

        self._recorder = CallRecorder(
            self._get_artifact_dir() / "segments",
            on_segment_closed=on_segment_closed,
        )

        def sink_callback(user: Member | User | None, data: VoiceData) -> None:
            if not user or not self._recorder:
                return

            opus_data = data.packet.decrypted_data
            if not opus_data:
                return

            # Decrypt DAVE (E2E encryption) layer
            dave_session = vc._connection.dave_session
            if dave_session and not dave_session.can_passthrough(user.id):
                try:
                    opus_data = dave_session.decrypt(
                        user.id, davey.MediaType.audio, opus_data
                    )
                except Exception:
                    return

            self._recorder.on_opus(user.id, opus_data)

        vc.listen(BasicSink(sink_callback, decode=False))

        # Start max-duration timeout
        self._timeout_task = asyncio.create_task(self._recording_timeout())
        log.info("Recording started")

    async def _recording_timeout(self) -> None:
        try:
            await asyncio.sleep(180 * 60)
            log.warning("Max recording duration reached, stopping")
            await self._stop_and_process()
        except asyncio.CancelledError:
            pass

    async def _transcribe_segment(
        self, user_id: int, offset: float, wav_path: Path
    ) -> None:
        """Transcribe a single WAV segment during recording."""
        self._add_manifest_entry(user_id, wav_path.name, offset)
        log.info(f"Streaming transcription started for {wav_path.name}")
        try:
            assert self._pipeline is not None
            text = await self._pipeline.transcribe_wav(wav_path) or ""
            self._set_manifest_text(wav_path.name, text)
            log.info(f"Streaming transcription complete for {wav_path.name}")
        except Exception:
            log.exception(f"Streaming transcription failed for {wav_path.name}")

    def _cancel_scheduled_tasks(self) -> None:
        """Cancel grace and timeout tasks, if running."""
        current = asyncio.current_task()
        for task in (self._grace_task, self._timeout_task):
            if task and task is not current and not task.done():
                task.cancel()
        self._grace_task = None
        self._timeout_task = None

    async def _stop_recording(self) -> tuple[CallRecorder, VoiceClient | None] | None:
        """Stop recording and disconnect from voice. Returns recorder + vc."""
        self._cancel_scheduled_tasks()

        recorder = self._recorder
        vc = self._voice_client
        self._recorder = None
        self._voice_client = None

        if not recorder:
            return None

        recorder.stop()
        if vc and vc.is_connected():
            await vc.disconnect()

        return recorder, vc

    async def _await_pending_transcriptions(self) -> None:
        """Wait for all in-flight streaming transcriptions to finish."""
        if self._pending_futures:
            log.info(f"Waiting for {len(self._pending_futures)} pending transcriptions")
            await asyncio.gather(
                *(asyncio.wrap_future(f) for f in self._pending_futures)
            )
            self._pending_futures = []

    async def _transcribe_remaining(self) -> None:
        """Transcribe manifest entries that have no text yet."""
        assert self._pipeline is not None
        manifest = self._load_manifest()
        segments_dir = self._get_artifact_dir() / "segments"

        for entries in manifest.values():
            remaining = [e for e in entries if "text" not in e]
            if remaining:
                log.info(f"Transcribing {len(remaining)} remaining segments")
                for entry in remaining:
                    wav_path = segments_dir / entry["file"]
                    text = await self._pipeline.transcribe_wav(wav_path) or ""
                    self._set_manifest_text(entry["file"], text)

    def _collect_segments(self) -> dict[int, list[tuple[float, str]]]:
        """Read the manifest and return all segments grouped by user ID."""
        manifest = self._load_manifest()
        segments: dict[int, list[tuple[float, str]]] = {}
        for uid_str, entries in manifest.items():
            user_id = int(uid_str)
            for entry in entries:
                if entry.get("text"):
                    segments.setdefault(user_id, []).append(
                        (entry["offset"], entry["text"])
                    )
        return segments

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
            text = text.replace(name, f"<@{user_id}>")
        return text

    async def _stop_and_process(self) -> None:
        result = await self._stop_recording()
        if not result:
            return
        recorder, _ = result

        if not self._pipeline:
            recorder.cleanup()
            return

        if recorder.speaker_count == 0:
            log.info("No speakers detected, discarding recording")
            recorder.cleanup()
            return

        try:
            await self._await_pending_transcriptions()

            user_segments = recorder.get_user_segments()
            if not user_segments:
                log.info("Empty recording, discarding")
                return

            # Register final segments (closed during get_user_segments)
            manifest = self._load_manifest()
            known_files: set[str] = set()
            for entries in manifest.values():
                for entry in entries:
                    known_files.add(entry["file"])
            for user_id, wav_segments in user_segments.items():
                for offset, path in wav_segments:
                    if path.name not in known_files:
                        self._add_manifest_entry(user_id, path.name, offset)

            await self._transcribe_remaining()

            all_segments = self._collect_segments()
            usernames = await self._resolve_usernames(set(user_segments))

            transcript = TranscriptionPipeline.format_transcript(
                all_segments, usernames
            )
            self._save_transcript(transcript)

            summary = await self._pipeline.summarize(transcript)
            if not summary:
                log.info("No substantive content, discarding")
                return

            summary = self._mentionify(summary, usernames)
            audio = await asyncio.to_thread(self._save_audio, user_segments)
            await self._post_results(transcript, summary, audio)
        except Exception as e:
            await self.bot.report_error(e)
        finally:
            recorder.cleanup()
            self._artifact_dir = None
            self._pending_futures = []

    def _get_artifact_dir(self) -> Path:
        """Get or create the artifact directory for the current recording."""
        if self._artifact_dir is None:
            timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M")
            self._artifact_dir = TRANSCRIPTIONS_DIR / timestamp
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        return self._artifact_dir

    def _save_audio(
        self,
        user_segments: dict[int, list[tuple[float, Path]]],
    ) -> Path:
        """Mix per-user WAV files into a single MP3."""
        out_dir = self._get_artifact_dir()

        mixed: AudioSegment | None = None
        for _uid, segments in user_segments.items():
            for offset, wav_path in segments:
                track = AudioSegment.from_wav(str(wav_path))
                if mixed is None:
                    mixed = AudioSegment.silent(duration=int(offset * 1000)) + track
                else:
                    end_ms = int(offset * 1000) + len(track)
                    if end_ms > len(mixed):
                        mixed += AudioSegment.silent(duration=end_ms - len(mixed))
                    mixed = mixed.overlay(track, position=int(offset * 1000))

        assert mixed is not None, "No audio segments to mix"
        path = out_dir / "recording.mp3"
        mixed = mixed.set_channels(1)
        mixed.export(path, format="mp3", bitrate="64k")
        log.info(f"Audio saved to {out_dir}")
        return path

    @property
    def _manifest_path(self) -> Path:
        return self._get_artifact_dir() / "segments" / "manifest.json"

    def _load_manifest(self) -> dict[str, Any]:
        """Load the manifest from disk, or return an empty dict."""
        if self._manifest_path.exists():
            result: dict[str, Any] = json.loads(
                self._manifest_path.read_text(encoding="utf-8")
            )
            return result
        return {}

    def _add_manifest_entry(self, user_id: int, wav_name: str, offset: float) -> None:
        """Register a WAV segment in the manifest (no text until transcribed)."""
        manifest = self._load_manifest()
        uid = str(user_id)
        if uid not in manifest:
            manifest[uid] = []
        manifest[uid].append({"file": wav_name, "offset": offset})
        self._manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def _set_manifest_text(self, wav_name: str, text: str) -> None:
        """Set the transcription text for an existing manifest entry."""
        manifest = self._load_manifest()
        for entries in manifest.values():
            for entry in entries:
                if entry["file"] == wav_name:
                    entry["text"] = text
                    self._manifest_path.write_text(
                        json.dumps(manifest, indent=2), encoding="utf-8"
                    )
                    return

    def _save_transcript(self, transcript: str) -> None:
        """Save transcript to disk."""
        out_dir = self._get_artifact_dir()
        (out_dir / "transcript.txt").write_text(transcript, encoding="utf-8")
        log.info(f"Transcript saved to {out_dir}")

    async def _post_results(
        self, transcript: str, summary: str, audio_path: Path
    ) -> None:
        channel_id = self._config.output_channel_id
        if not channel_id:
            log.warning("No output channel configured")
            return

        channel = await self.bot.get_or_fetch_channel(channel_id)
        assert isinstance(channel, Messageable)

        embed = Embed(title="Voice Call Summary", description=summary[:4096])

        files = [
            File(audio_path, filename="recording.mp3"),
            TextFile(transcript, "transcript.txt"),
        ]
        await channel.send(embed=embed, files=files)
        log.info("Transcript and summary posted")

    async def cog_unload(self) -> None:
        self._cancel_grace()
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        if self._voice_client and self._voice_client.is_connected():
            await self._voice_client.disconnect()
        if self._recorder:
            self._recorder.stop()
            self._recorder.cleanup()


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(VoiceSummary(bot))
