from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

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

# Resolved against the repo root so the location doesn't depend on cwd.
# session.py lives at <repo>/rocketwatch/plugins/voice_summary/session.py
TRANSCRIPTIONS_DIR = Path(__file__).resolve().parents[3] / "voice_calls"


class SegmentEntry(TypedDict):
    file: str
    offset: float
    text: NotRequired[str]


Manifest = dict[int, list[SegmentEntry]]


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
        self._manifest: Manifest = {}
        self._manifest_lock = asyncio.Lock()
        self._pending_tasks: set[asyncio.Task[None]] = set()

    def _ensure_artifact_dir(self) -> Path:
        if self._artifact_dir is None:
            timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M")
            self._artifact_dir = TRANSCRIPTIONS_DIR / timestamp
            self._artifact_dir.mkdir(parents=True, exist_ok=True)
        return self._artifact_dir

    def _segments_dir(self) -> Path:
        return self._ensure_artifact_dir() / "segments"

    def _manifest_path(self) -> Path:
        return self._segments_dir() / "manifest.json"

    async def start(self, vc: VoiceRecvClient) -> None:
        """Begin recording on a voice client, waiting for connection if needed."""
        loop = asyncio.get_running_loop()

        def on_segment_closed(user_id: int, offset: float, wav_path: Path) -> None:
            # Called from the recorder's thread; hop to the loop before touching state.
            loop.call_soon_threadsafe(
                self._schedule_transcription, user_id, offset, wav_path
            )

        self.recorder = CallRecorder(
            self._segments_dir(),
            on_segment_closed=on_segment_closed,
        )
        await self._attach_sink(vc)
        log.info("Recording started")

    def _schedule_transcription(
        self, user_id: int, offset: float, wav_path: Path
    ) -> None:
        task = asyncio.create_task(self._transcribe_segment(user_id, offset, wav_path))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

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

            self.recorder.on_opus(user.id, opus_data, data.packet.timestamp)

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
        await self._add_manifest_entry(user_id, wav_path.name, offset)
        log.info(f"Streaming transcription started for {wav_path.name}")
        try:
            text = await self._pipeline.transcribe_wav(wav_path)
            await self._set_manifest_text(user_id, wav_path.name, text)
            log.info(f"Streaming transcription complete for {wav_path.name}")
        except Exception:
            log.exception(f"Streaming transcription failed for {wav_path.name}")

    async def await_pending_transcriptions(self) -> None:
        """Wait for all in-flight streaming transcriptions to finish."""
        while self._pending_tasks:
            tasks = list(self._pending_tasks)
            log.info(f"Waiting for {len(tasks)} pending transcriptions")
            await asyncio.gather(*tasks, return_exceptions=True)

    async def transcribe_remaining(self) -> None:
        """Transcribe manifest entries that have no text yet."""
        async with self._manifest_lock:
            remaining = [
                (user_id, entry["file"])
                for user_id, entries in self._manifest.items()
                for entry in entries
                if "text" not in entry
            ]

        if not remaining:
            return

        log.info(f"Transcribing {len(remaining)} remaining segments")
        segments_dir = self._segments_dir()
        for user_id, wav_name in remaining:
            wav_path = segments_dir / wav_name
            try:
                text = await self._pipeline.transcribe_wav(wav_path)
            except Exception:
                log.exception(f"Final transcription failed for {wav_name}")
                continue
            await self._set_manifest_text(user_id, wav_name, text)

    async def register_final_segments(
        self, user_segments: dict[int, list[tuple[float, Path]]]
    ) -> None:
        """Register segments closed during finalization that aren't in the manifest."""
        async with self._manifest_lock:
            known = {e["file"] for entries in self._manifest.values() for e in entries}
            dirty = False
            for user_id, wav_segments in user_segments.items():
                for offset, path in wav_segments:
                    if path.name not in known:
                        self._manifest.setdefault(user_id, []).append(
                            {"file": path.name, "offset": offset}
                        )
                        dirty = True
            if dirty:
                self._flush_manifest_locked()

    def collect_segments(self) -> dict[int, list[tuple[float, str]]]:
        """Return all transcribed segments by user ID."""
        segments: dict[int, list[tuple[float, str]]] = {}
        for user_id, entries in self._manifest.items():
            for entry in entries:
                if text := entry.get("text"):
                    segments.setdefault(user_id, []).append((entry["offset"], text))
        return segments

    def save_transcript(self, transcript: str) -> None:
        """Save transcript to disk."""
        out = self._ensure_artifact_dir() / "transcript.txt"
        out.write_text(transcript, encoding="utf-8")
        log.info(f"Transcript saved to {out.parent}")

    def mix_audio(self, user_segments: dict[int, list[tuple[float, Path]]]) -> Path:
        """Mix per-user WAV files into a single MP3."""
        tracks: list[tuple[int, AudioSegment]] = []
        total_ms = 0
        for segments in user_segments.values():
            for offset, wav_path in segments:
                track = AudioSegment.from_wav(str(wav_path))
                start_ms = int(offset * 1000)
                tracks.append((start_ms, track))
                total_ms = max(total_ms, start_ms + len(track))

        mixed = AudioSegment.silent(duration=total_ms)
        for start_ms, track in tracks:
            mixed = mixed.overlay(track, position=start_ms)

        mixed = mixed.set_channels(1)
        out = self._ensure_artifact_dir() / "recording.mp3"
        mixed.export(out, format="mp3", bitrate="64k")
        log.info(f"Audio saved to {out.parent}")
        return out

    def _flush_manifest_locked(self) -> None:
        """Write the in-memory manifest to disk. Caller must hold _manifest_lock."""
        serializable = {str(uid): entries for uid, entries in self._manifest.items()}
        self._manifest_path().write_text(
            json.dumps(serializable, indent=2), encoding="utf-8"
        )

    async def _add_manifest_entry(
        self, user_id: int, wav_name: str, offset: float
    ) -> None:
        async with self._manifest_lock:
            self._manifest.setdefault(user_id, []).append(
                {"file": wav_name, "offset": offset}
            )
            self._flush_manifest_locked()

    async def _set_manifest_text(self, user_id: int, wav_name: str, text: str) -> None:
        async with self._manifest_lock:
            for entry in self._manifest.get(user_id, []):
                if entry["file"] == wav_name:
                    entry["text"] = text
                    self._flush_manifest_locked()
                    return
            log.warning(
                f"Tried to set text for unknown manifest entry: "
                f"user={user_id} file={wav_name}"
            )

    async def _resolve_usernames(
        self, guild_id: int, user_ids: set[int]
    ) -> dict[int, str]:
        """Resolve user IDs to display names, falling back to the global user."""

        async def resolve_one(user_id: int) -> str:
            try:
                member = await self._bot.get_or_fetch_member(guild_id, user_id)
                return member.display_name
            except Exception:
                pass
            try:
                user = await self._bot.get_or_fetch_user(user_id)
                return user.display_name
            except Exception:
                return str(user_id)

        return {uid: await resolve_one(uid) for uid in user_ids}

    async def _prepare_segments(
        self, recorder: CallRecorder
    ) -> (
        tuple[
            dict[int, list[tuple[float, Path]]],
            dict[int, list[tuple[float, str]]],
        ]
        | None
    ):
        """Flush recorder state through the pipeline and return (wav, text) segments."""
        await self.await_pending_transcriptions()

        wav_segments = recorder.get_user_segments()
        if not wav_segments:
            return None

        await self.register_final_segments(wav_segments)
        await self.transcribe_remaining()
        return wav_segments, self.collect_segments()

    async def _build_artifacts(
        self,
        wav_segments: dict[int, list[tuple[float, Path]]],
        text_segments: dict[int, list[tuple[float, str]]],
        usernames: dict[int, str],
    ) -> CallResult | None:
        transcript = TranscriptionPipeline.format_transcript(text_segments, usernames)
        self.save_transcript(transcript)

        summary = await self._pipeline.summarize(transcript, usernames)
        audio = await asyncio.to_thread(self.mix_audio, wav_segments)

        if not summary:
            log.info("No substantive content, discarding")
            return None

        return CallResult(
            transcript=transcript,
            summary=summary,
            audio_path=audio,
        )

    async def finalize(self) -> CallResult | None:
        """Stop recording, transcribe, and produce the final transcript and summary.

        Returns None if there is nothing substantive to report.
        """
        recorder, vc = await self.stop()
        if not (vc and recorder and recorder.speaker_count > 0):
            if recorder and (recorder.speaker_count == 0):
                log.info("No speakers detected, discarding recording")
            return None

        prepared = await self._prepare_segments(recorder)
        if prepared is None:
            log.info("Empty recording, discarding")
            return None

        wav_segments, text_segments = prepared
        usernames = await self._resolve_usernames(
            vc.channel.guild.id, set(wav_segments)
        )
        return await self._build_artifacts(wav_segments, text_segments, usernames)
