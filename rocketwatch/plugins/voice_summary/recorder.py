import contextlib
import logging
import threading
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
FRAME_SIZE = CHANNELS * SAMPLE_WIDTH  # bytes per PCM frame
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH

SILENCE_DURATION = 1.0  # seconds of packet gap before splitting
MAX_SEGMENT_DURATION = 60.0  # seconds before forcing a split

SILENCE_DURATION_SAMPLES = int(SILENCE_DURATION * SAMPLE_RATE)
MAX_SEGMENT_DURATION_SAMPLES = int(MAX_SEGMENT_DURATION * SAMPLE_RATE)


class _PendingSegment:
    """A segment being accumulated in memory.

    Packets are stored keyed by RTP timestamp (dedup) and sorted at finalize
    time, so out-of-order arrivals land in their correct slot without needing
    any streaming-write reorder logic.
    """

    __slots__ = (
        "call_time",
        "max_rtp_end",
        "packets",
        "rtp_start",
        "stat_drops",
        "stat_pkts",
    )

    def __init__(self, rtp_start: int, call_time: float) -> None:
        self.rtp_start = rtp_start
        self.max_rtp_end = rtp_start
        self.call_time = call_time
        self.packets: dict[int, bytes] = {}
        self.stat_pkts = 0
        self.stat_drops = 0


class UserStream:
    """Buffers PCM audio for a single user, writing WAV files to disk."""

    def __init__(
        self,
        user_id: int,
        out_dir: Path,
        recorder_start_time: float,
        on_segment_closed: Callable[[int, float, Path], None] | None = None,
    ) -> None:
        self.user_id = user_id
        self._out_dir = out_dir
        self._recorder_start_time = recorder_start_time
        self._on_segment_closed = on_segment_closed
        self._segment_index = 0
        self._segments: list[tuple[float, Path]] = []
        # Maps per-user RTP sample clock to call-relative seconds. Set on the
        # first packet from this user (or after an SSRC change).
        self._rtp_anchor: int | None = None
        self._wall_anchor: float = 0.0
        # Serializes sink-thread writes against main-thread close/flush.
        self._lock = threading.Lock()
        self._closed = False
        # Currently-active in-memory segment. Packets arrive here regardless
        # of order; it's sorted + written to disk on finalize.
        self._open: _PendingSegment | None = None

    def _rtp_to_call_time(self, rtp_ts: int) -> float:
        """Convert an RTP sample-clock timestamp to seconds from call start."""
        assert self._rtp_anchor is not None
        return self._wall_anchor + (rtp_ts - self._rtp_anchor) / SAMPLE_RATE

    def _anchor_rtp(self, rtp_ts: int) -> None:
        """Peg this stream's RTP clock to the current call time."""
        self._rtp_anchor = rtp_ts
        self._wall_anchor = time.monotonic() - self._recorder_start_time

    def write(self, pcm: bytes, rtp_timestamp: int) -> None:
        with self._lock:
            if self._closed:
                return

            if self._rtp_anchor is None:
                self._anchor_rtp(rtp_timestamp)

            # SSRC reset / RTP wraparound: if the packet's RTP is implausibly
            # far from our current open segment, start over with a new anchor.
            if self._open is not None and (
                rtp_timestamp - self._open.max_rtp_end
                > 2 * MAX_SEGMENT_DURATION_SAMPLES
                or self._open.rtp_start - rtp_timestamp
                > 2 * MAX_SEGMENT_DURATION_SAMPLES
            ):
                self._finalize(self._open)
                self._open = None
                self._anchor_rtp(rtp_timestamp)

            call_time = self._rtp_to_call_time(rtp_timestamp)
            samples = len(pcm) // FRAME_SIZE
            rtp_end = rtp_timestamp + samples

            if self._open is None:
                self._open = _PendingSegment(rtp_timestamp, call_time)
            else:
                seg = self._open
                if rtp_timestamp < seg.rtp_start:
                    # Late arrival for a packet older than the segment start.
                    # It belongs to a previous, already-finalized segment;
                    # can't go back and edit that file.
                    seg.stat_drops += 1
                    return
                over_max = rtp_timestamp - seg.rtp_start >= MAX_SEGMENT_DURATION_SAMPLES
                silence_gap = rtp_timestamp > seg.max_rtp_end + SILENCE_DURATION_SAMPLES
                if over_max or silence_gap:
                    self._finalize(seg)
                    self._open = _PendingSegment(rtp_timestamp, call_time)

            seg = self._open
            seg.packets[rtp_timestamp] = pcm
            seg.stat_pkts += 1
            if rtp_end > seg.max_rtp_end:
                seg.max_rtp_end = rtp_end

    def _finalize(self, seg: _PendingSegment) -> None:
        """Sort seg's packets by RTP and write the segment's wav to disk."""
        if not seg.packets:
            return

        sorted_packets = sorted(seg.packets.items())
        path = self._out_dir / f"{self.user_id}_{self._segment_index}.wav"
        self._segment_index += 1

        pcm_samples = 0
        pad_samples = 0
        pad_events = 0
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(SAMPLE_WIDTH)
            wav.setframerate(SAMPLE_RATE)

            last_end_rtp = sorted_packets[0][0]
            for rtp, pcm in sorted_packets:
                if rtp > last_end_rtp:
                    gap_samples = rtp - last_end_rtp
                    wav.writeframes(b"\x00" * gap_samples * FRAME_SIZE)
                    pad_samples += gap_samples
                    pad_events += 1
                elif rtp < last_end_rtp:
                    # Overlap from a packet whose audio extent reached into
                    # another packet's slot. Skip the overlapping prefix.
                    skip_frames = last_end_rtp - rtp
                    skip_bytes = skip_frames * FRAME_SIZE
                    if skip_bytes >= len(pcm):
                        seg.stat_drops += 1
                        continue
                    pcm = pcm[skip_bytes:]
                wav.writeframes(pcm)
                written_samples = len(pcm) // FRAME_SIZE
                last_end_rtp = rtp + written_samples
                pcm_samples += written_samples

        self._segments.append((seg.call_time, path))
        self._log_segment_stats(path, seg, pcm_samples, pad_samples, pad_events)
        if self._on_segment_closed:
            self._on_segment_closed(self.user_id, seg.call_time, path)

    def _log_segment_stats(
        self,
        path: Path,
        seg: _PendingSegment,
        pcm_samples: int,
        pad_samples: int,
        pad_events: int,
    ) -> None:
        pcm_s = pcm_samples / SAMPLE_RATE
        pad_s = pad_samples / SAMPLE_RATE
        total_s = pcm_s + pad_s
        pct = pad_s / total_s * 100 if total_s else 0.0
        log.info(
            f"Segment {path.name}: pkts={seg.stat_pkts} "
            f"pcm={pcm_s:.2f}s pad={pad_s:.2f}s ({pct:.1f}%) "
            f"pad_events={pad_events} drops={seg.stat_drops}"
        )

    def get_segments(self) -> list[tuple[float, Path]]:
        """Return list of (offset_seconds, wav_path) for each segment."""
        with self._lock:
            if self._open is not None:
                self._finalize(self._open)
                self._open = None
            return list(self._segments)

    def close(self) -> None:
        with self._lock:
            if self._open is not None:
                self._finalize(self._open)
                self._open = None
            self._closed = True


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
        # Counts of packets rejected before they reach a UserStream.
        self._drops_empty = 0
        self._drops_decode = 0

    def on_opus(self, user_id: int, opus_data: bytes, rtp_timestamp: int) -> None:
        """Receive an Opus packet, decode to PCM, and buffer it.

        rtp_timestamp is the packet's RTP timestamp (48 kHz sample clock).
        It is used directly for intra-stream timing so network jitter doesn't
        show up as spurious silence or segment splits.
        """
        if not self._recording:
            return
        if not opus_data:
            self._drops_empty += 1
            return

        if user_id not in self._decoders:
            log.info(f"Receiving audio from user {user_id}")
            self._decoders[user_id] = OpusDecoder()
            self._streams[user_id] = UserStream(
                user_id,
                self._out_dir,
                self._start_time,
                self._on_segment_closed,
            )

        try:
            pcm = self._decoders[user_id].decode(opus_data, fec=False)
        except Exception:
            log.debug(
                f"Failed to decode Opus for user {user_id}: "
                f"len={len(opus_data)} first_bytes={opus_data[:16].hex()}"
            )
            self._drops_decode += 1
            return

        self._streams[user_id].write(pcm, rtp_timestamp)

    def stop(self) -> None:
        """Stop accepting packets and flush every stream's open wav to disk.

        Final segments close through the same on_segment_closed path as mid-call
        rotations, so there's no separate finalize-time transcription code path.
        """
        self._recording = False
        for stream in self._streams.values():
            with contextlib.suppress(Exception):
                stream.close()

    @property
    def speaker_count(self) -> int:
        return len(self._streams)

    def get_user_segments(self) -> dict[int, list[tuple[float, Path]]]:
        """Return per-user WAV file paths with their start offsets."""
        result: dict[int, list[tuple[float, Path]]] = {}
        for user_id, stream in self._streams.items():
            segments = stream.get_segments()
            if segments:
                result[user_id] = segments
        return result

    def cleanup(self) -> None:
        """Close open file handles."""
        for stream in self._streams.values():
            with contextlib.suppress(Exception):
                stream.close()
