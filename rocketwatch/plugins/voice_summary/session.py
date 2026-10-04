from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

import davey
import lameenc
import numpy as np
import soundfile as sf
from discord import Member, VoiceClient
from discord.ext.voice_recv import BasicSink, VoiceRecvClient

from rocketwatch.plugins.voice_summary.pipeline import TranscriptionPipeline
from rocketwatch.plugins.voice_summary.recorder import SAMPLE_RATE, CallRecorder

if TYPE_CHECKING:
    from discord import User
    from discord.ext.voice_recv.opus import VoiceData

    from rocketwatch.bot import RocketWatch

log = logging.getLogger("rocketwatch.voice_summary.session")

# resolved against the repo root so the location doesn't depend on cwd.
TRANSCRIPTIONS_DIR = Path(__file__).resolve().parents[3] / "voice_calls"

MIX_WINDOW_SAMPLES = 60 * SAMPLE_RATE


def _read_mono(wav_path: Path, start: int, stop: int) -> np.ndarray:
    """Read frames [start, stop) of a WAV, downmixed to mono."""
    data, _ = sf.read(
        str(wav_path), start=start, stop=stop, dtype="int16", always_2d=True
    )
    mono: np.ndarray = data.sum(axis=1, dtype=np.int32) // data.shape[1]
    return mono


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
        self._dave_failures = 0

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
                    self._dave_failures += 1
                    return

            self.recorder.on_opus(user.id, opus_data, data.packet.timestamp)

        # a client reconnected by discord.py keeps its reader
        if not vc.is_listening():
            vc.listen(BasicSink(sink_callback, decode=False))

    async def stop(self) -> tuple[CallRecorder | None, VoiceClient | None]:
        """Stop recording and disconnect. Returns the recorder if active."""
        recorder = self.recorder
        vc = self.voice_client
        self.recorder = None
        self.voice_client = None

        if recorder:
            recorder.stop()
        if self._dave_failures:
            log.warning(f"{self._dave_failures} packets failed DAVE decryption")

        if vc and vc.is_connected():
            await vc.disconnect()
        elif vc and vc.guild.voice_client is vc:
            # mid-reconnect: force, or discord.py rejoins after we've finalized
            await vc.disconnect(force=True)

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
        """Wait for all in-flight streaming transcriptions to finish.

        Yields once before and after gather so any transcription tasks
        scheduled via call_soon_threadsafe (e.g. from final-segment closes
        during recorder.stop) are picked up before we return.
        """
        await asyncio.sleep(0)
        while self._pending_tasks:
            tasks = list(self._pending_tasks)
            log.info(f"Waiting for {len(tasks)} pending transcriptions")
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)

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
        """Mix per-user WAV files into a single mono MP3, one window at a time."""
        # (start, length) in samples
        tracks: list[tuple[int, int, Path]] = []
        for segments in user_segments.values():
            for offset, wav_path in segments:
                length = sf.info(str(wav_path)).frames
                tracks.append((int(offset * SAMPLE_RATE), length, wav_path))
        total_samples = max((start + n for start, n, _ in tracks), default=0)

        out = self._ensure_artifact_dir() / "recording.mp3"
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(64)
        encoder.set_in_sample_rate(SAMPLE_RATE)
        encoder.set_channels(1)
        with out.open("wb") as f:
            for w0 in range(0, total_samples, MIX_WINDOW_SAMPLES):
                w1 = min(w0 + MIX_WINDOW_SAMPLES, total_samples)
                # sum in int32 to give summed samples headroom, then saturate.
                window = np.zeros(w1 - w0, dtype=np.int32)
                for start, n, wav_path in tracks:
                    a, b = max(w0, start), min(w1, start + n)
                    if a < b:
                        window[a - w0 : b - w0] += _read_mono(
                            wav_path, a - start, b - start
                        )
                np.clip(
                    window, np.iinfo(np.int16).min, np.iinfo(np.int16).max, out=window
                )
                f.write(encoder.encode(window.astype(np.int16)))
            f.write(encoder.flush())
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

        return wav_segments, self.collect_segments()

    async def _build_artifacts(
        self,
        wav_segments: dict[int, list[tuple[float, Path]]],
        text_segments: dict[int, list[tuple[float, str]]],
        usernames: dict[int, str],
    ) -> CallResult | None:
        transcript = TranscriptionPipeline.format_transcript(text_segments, usernames)
        self.save_transcript(transcript)

        summary, audio = await asyncio.gather(
            self._pipeline.summarize(transcript, usernames),
            asyncio.to_thread(self.mix_audio, wav_segments),
        )

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
