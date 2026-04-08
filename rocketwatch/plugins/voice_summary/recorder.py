import contextlib
import logging
import time
import wave
from collections.abc import Callable
from pathlib import Path

from discord.opus import Decoder as OpusDecoder

log = logging.getLogger("rocketwatch.voice_summary.recorder")

# Discord audio: 48kHz, 16-bit signed, stereo
SAMPLE_RATE = OpusDecoder.SAMPLING_RATE  # 48000
CHANNELS = OpusDecoder.CHANNELS  # 2
SAMPLE_WIDTH = OpusDecoder.SAMPLE_SIZE // OpusDecoder.CHANNELS  # 2 bytes (16-bit)
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH

MIN_SEGMENT_DURATION = 1.0  # seconds
MIN_SEGMENT_BYTES = int(MIN_SEGMENT_DURATION * BYTES_PER_SECOND) + 44  # +WAV header

SILENCE_DURATION = 2.0  # seconds of packet gap before splitting
MAX_SEGMENT_DURATION = 60.0  # seconds before forcing a split


class UserStream:
    """Buffers PCM audio for a single user, writing WAV files to disk."""

    def __init__(
        self,
        user_id: int,
        out_dir: Path,
        on_segment_closed: Callable[[int, float, Path], None] | None = None,
    ) -> None:
        self.user_id = user_id
        self._out_dir = out_dir
        self._on_segment_closed = on_segment_closed
        self._segment_index = 0
        self._wav: wave.Wave_write | None = None
        self._segment_start: float = 0.0
        self._last_timestamp: float = 0.0
        self._segments: list[tuple[float, Path]] = []  # (offset, path)

    def _start_segment(self, timestamp: float) -> None:
        self._close_wav()
        path = self._out_dir / f"user_{self.user_id}_{self._segment_index}.wav"
        self._wav = wave.open(str(path), "wb")  # noqa: SIM115
        self._wav.setnchannels(CHANNELS)
        self._wav.setsampwidth(SAMPLE_WIDTH)
        self._wav.setframerate(SAMPLE_RATE)
        self._segment_start = timestamp
        self._last_timestamp = timestamp
        self._segments.append((timestamp, path))
        self._segment_index += 1

    def write(self, pcm: bytes, timestamp: float) -> None:
        if not self._wav:
            self._start_segment(timestamp)
        else:
            # Start a new segment on packet gap or max duration
            expected = self._last_timestamp + len(pcm) / BYTES_PER_SECOND
            if (
                timestamp - expected > SILENCE_DURATION
                or timestamp - self._segment_start >= MAX_SEGMENT_DURATION
            ):
                self._start_segment(timestamp)

        self._last_timestamp = timestamp
        assert self._wav is not None
        self._wav.writeframes(pcm)

    def get_segments(self) -> list[tuple[float, Path]]:
        """Return list of (offset_seconds, wav_path) for each segment."""
        self._close_wav()
        return list(self._segments)

    def _close_wav(self) -> None:
        if self._wav:
            self._wav.close()
            self._wav = None
            if self._on_segment_closed and self._segments:
                offset, path = self._segments[-1]
                if path.stat().st_size >= MIN_SEGMENT_BYTES:
                    self._on_segment_closed(self.user_id, offset, path)

    def close(self) -> None:
        self._close_wav()


class CallRecorder:
    """Records per-user PCM audio from a Discord voice channel.

    Receives raw Opus packets and decodes them to PCM internally,
    bypassing discord-ext-voice-recv's broken Opus decoder.
    """

    def __init__(
        self,
        out_dir: Path,
        start_time: float | None = None,
        on_segment_closed: Callable[[int, float, Path], None] | None = None,
    ) -> None:
        self._out_dir = out_dir
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._streams: dict[int, UserStream] = {}
        self._decoders: dict[int, OpusDecoder] = {}
        self._start_time = start_time or time.monotonic()
        self._on_segment_closed = on_segment_closed
        self._recording = True

    def on_opus(self, user_id: int, opus_data: bytes) -> None:
        """Receive an Opus packet, decode to PCM, and buffer it."""
        if not self._recording or not opus_data:
            return

        if user_id not in self._decoders:
            log.info(f"Receiving audio from user {user_id}")
            self._decoders[user_id] = OpusDecoder()
            self._streams[user_id] = UserStream(
                user_id, self._out_dir, self._on_segment_closed
            )

        try:
            pcm = self._decoders[user_id].decode(opus_data, fec=False)
        except Exception:
            log.debug(
                f"Failed to decode Opus for user {user_id}: "
                f"len={len(opus_data)} first_bytes={opus_data[:16].hex()}"
            )
            return

        timestamp = time.monotonic() - self._start_time
        self._streams[user_id].write(pcm, timestamp)

    def stop(self) -> None:
        self._recording = False

    @property
    def speaker_count(self) -> int:
        return len(self._streams)

    def get_user_segments(self) -> dict[int, list[tuple[float, Path]]]:
        """Return per-user WAV file paths with their start offsets.

        Suppresses the on_segment_closed callback to avoid duplicate
        transcription of final segments that are closed during finalization.
        """
        for stream in self._streams.values():
            stream._on_segment_closed = None
        result: dict[int, list[tuple[float, Path]]] = {}
        for user_id, stream in self._streams.items():
            segments = [
                (offset, path)
                for offset, path in stream.get_segments()
                if path.stat().st_size >= MIN_SEGMENT_BYTES
            ]
            if segments:
                result[user_id] = segments

        return result

    def cleanup(self) -> None:
        """Close open file handles."""
        for stream in self._streams.values():
            with contextlib.suppress(Exception):
                stream.close()
