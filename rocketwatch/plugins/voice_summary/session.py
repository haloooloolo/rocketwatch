from __future__ import annotations

import asyncio
import json
import logging
import re
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import davey
from discord import Member, VoiceClient
from discord.ext.voice_recv import BasicSink, VoiceRecvClient
from pydub import AudioSegment

from rocketwatch.plugins.voice_summary.pipeline import TranscriptionPipeline
from rocketwatch.plugins.voice_summary.recorder import CallRecorder

if TYPE_CHECKING:
    from discord import User
    from discord.ext.voice_recv.opus import VoiceData

    from rocketwatch.bot import RocketWatch

log = logging.getLogger("rocketwatch.voice_summary.session")

TRANSCRIPTIONS_DIR = Path("../voice_calls")


@dataclass
class CallResult:
    transcript: str
    summary: str
    audio_path: Path


class CallSession:
    """Encapsulates all state and artifact management for a single voice call."""

    def __init__(self, pipeline: TranscriptionPipeline, bot: RocketWatch) -> None:
        self._pipeline = pipeline
        self._bot = bot
        self.recorder: CallRecorder | None = None
        self.voice_client: VoiceClient | None = None
        self._artifact_dir: Path | None = None
        self._pending_futures: list[Future[None]] = []

    @property
    def artifact_dir(self) -> Path:
        """Get or create the artifact directory for this session."""
        if self._artifact_dir is None:
            timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M")
            self._artifact_dir = TRANSCRIPTIONS_DIR / timestamp
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        return self._artifact_dir

    @property
    def _manifest_path(self) -> Path:
        return self.artifact_dir / "segments" / "manifest.json"

    async def start(self, vc: VoiceRecvClient) -> None:
        """Begin recording on a voice client, waiting for connection if needed."""
        loop = asyncio.get_running_loop()

        def on_segment_closed(user_id: int, offset: float, wav_path: Path) -> None:
            future = asyncio.run_coroutine_threadsafe(
                self._transcribe_segment(user_id, offset, wav_path), loop
            )
            self._pending_futures.append(future)

        self.recorder = CallRecorder(
            self.artifact_dir / "segments",
            on_segment_closed=on_segment_closed,
        )
        await self._attach_sink(vc)
        log.info("Recording started")

    async def resume(self, vc: VoiceRecvClient) -> None:
        """Re-attach recording to a new voice client after an unexpected disconnect."""
        if not self.recorder:
            raise RuntimeError("Cannot resume: no active recording")
        await self._attach_sink(vc)
        log.info("Recording resumed")

    async def _attach_sink(self, vc: VoiceRecvClient) -> None:
        self.voice_client = vc
        if not vc.is_connected():
            connected = await asyncio.to_thread(vc.wait_until_connected)
            if not connected:
                raise ConnectionError("Voice client failed to connect")

        def sink_callback(user: Member | User | None, data: VoiceData) -> None:
            if not user or not self.recorder:
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

            self.recorder.on_opus(user.id, opus_data)

        vc.listen(BasicSink(sink_callback, decode=False))

    async def stop(self) -> tuple[CallRecorder | None, VoiceClient | None]:
        """Stop recording and disconnect. Returns the recorder if active."""
        recorder = self.recorder
        vc = self.voice_client
        self.recorder = None
        self.voice_client = None

        if recorder:
            recorder.stop()

        if vc and vc.is_connected():
            await vc.disconnect()

        return recorder, vc

    async def _transcribe_segment(
        self, user_id: int, offset: float, wav_path: Path
    ) -> None:
        """Transcribe a single WAV segment during recording."""
        self._add_manifest_entry(user_id, wav_path.name, offset)
        log.info(f"Streaming transcription started for {wav_path.name}")
        try:
            text = await self._pipeline.transcribe_wav(wav_path)
            self._set_manifest_text(user_id, wav_path.name, text)
            log.info(f"Streaming transcription complete for {wav_path.name}")
        except Exception:
            log.exception(f"Streaming transcription failed for {wav_path.name}")

    async def await_pending_transcriptions(self) -> None:
        """Wait for all in-flight streaming transcriptions to finish."""
        if self._pending_futures:
            log.info(f"Waiting for {len(self._pending_futures)} pending transcriptions")
            await asyncio.gather(
                *(asyncio.wrap_future(f) for f in self._pending_futures)
            )
            self._pending_futures = []

    async def transcribe_remaining(self) -> None:
        """Transcribe manifest entries that have no text yet."""
        manifest = self._load_manifest()
        segments_dir = self.artifact_dir / "segments"

        remaining: list[tuple[int, str]] = []
        for uid_str, entries in manifest.items():
            user_id = int(uid_str)
            remaining.extend([(user_id, e["file"]) for e in entries if "text" not in e])

        if not remaining:
            return

        log.info(f"Transcribing {len(remaining)} remaining segments")
        for user_id, wav_name in remaining:
            wav_path = segments_dir / wav_name
            text = await self._pipeline.transcribe_wav(wav_path)
            self._set_manifest_text(user_id, wav_name, text)

    def register_final_segments(
        self, user_segments: dict[int, list[tuple[float, Path]]]
    ) -> None:
        """Register segments closed during finalization that aren't in the manifest."""
        manifest = self._load_manifest()
        known_files: set[str] = set()
        for entries in manifest.values():
            for entry in entries:
                known_files.add(entry["file"])
        for user_id, wav_segments in user_segments.items():
            for offset, path in wav_segments:
                if path.name not in known_files:
                    self._add_manifest_entry(user_id, path.name, offset)

    def collect_segments(self) -> dict[int, list[tuple[float, str]]]:
        """Read the manifest and return all transcribed segments by user ID."""
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

    def save_transcript(self, transcript: str) -> None:
        """Save transcript to disk."""
        (self.artifact_dir / "transcript.txt").write_text(transcript, encoding="utf-8")
        log.info(f"Transcript saved to {self.artifact_dir}")

    def mix_audio(self, user_segments: dict[int, list[tuple[float, Path]]]) -> Path:
        """Mix per-user WAV files into a single MP3."""
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
        path = self.artifact_dir / "recording.mp3"
        mixed = mixed.set_channels(1)
        mixed.export(path, format="mp3", bitrate="64k")
        log.info(f"Audio saved to {self.artifact_dir}")
        return path

    def _load_manifest(self) -> dict[str, Any]:
        if self._manifest_path.exists():
            result: dict[str, Any] = json.loads(
                self._manifest_path.read_text(encoding="utf-8")
            )
            return result
        return {}

    def _add_manifest_entry(self, user_id: int, wav_name: str, offset: float) -> None:
        manifest = self._load_manifest()
        uid = str(user_id)
        if uid not in manifest:
            manifest[uid] = []
        manifest[uid].append({"file": wav_name, "offset": offset})
        self._manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def _set_manifest_text(self, user_id: int, wav_name: str, text: str) -> None:
        manifest = self._load_manifest()
        for entry in manifest.get(str(user_id), []):
            if entry["file"] == wav_name:
                entry["text"] = text
                self._manifest_path.write_text(
                    json.dumps(manifest, indent=2), encoding="utf-8"
                )
                return

    @staticmethod
    def _mentionify(text: str, usernames: dict[int, str]) -> str:
        """Replace display names with Discord mentions."""
        for user_id, name in sorted(
            usernames.items(), key=lambda x: len(x[1]), reverse=True
        ):
            text = re.sub(re.escape(name), f"<@{user_id}>", text, flags=re.IGNORECASE)
        return text

    async def _resolve_usernames(
        self, guild_id: int, user_ids: set[int]
    ) -> dict[int, str]:
        """Resolve user IDs to display names, falling back to the global user."""
        usernames: dict[int, str] = {}

        for user_id in user_ids:
            usernames[user_id] = str(user_id)
            try:
                member = await self._bot.get_or_fetch_member(guild_id, user_id)
                usernames[user_id] = member.display_name
                continue
            except Exception:
                pass
            try:
                user = await self._bot.get_or_fetch_user(user_id)
                usernames[user_id] = user.display_name
                continue
            except Exception:
                pass

        return usernames

    async def finalize(self) -> CallResult | None:
        """Stop recording, transcribe, and produce the final transcript and summary.

        Returns None if there is nothing substantive to report.
        """
        recorder, vc = await self.stop()
        if not (vc and recorder and recorder.speaker_count > 0):
            if recorder and (recorder.speaker_count == 0):
                log.info("No speakers detected, discarding recording")
            return None

        await self.await_pending_transcriptions()

        user_segments = recorder.get_user_segments()
        if not user_segments:
            log.info("Empty recording, discarding")
            return None

        self.register_final_segments(user_segments)
        await self.transcribe_remaining()

        all_segments = self.collect_segments()
        usernames = await self._resolve_usernames(
            vc.channel.guild.id, set(user_segments)
        )

        transcript = TranscriptionPipeline.format_transcript(all_segments, usernames)
        self.save_transcript(transcript)

        summary = await self._pipeline.summarize(transcript)
        audio = await asyncio.to_thread(self.mix_audio, user_segments)

        if not summary:
            log.info("No substantive content, discarding")
            return None

        summary = self._mentionify(summary, usernames)

        return CallResult(
            transcript=transcript,
            summary=summary,
            audio_path=audio,
        )
