import contextlib
import logging
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from discord.opus import Decoder as OpusDecoder


class _OpusDecoderLike(Protocol):
    """Structural subset of ``discord.opus.Decoder`` the recorder relies on.

    Defined here so tests can swap in a libopus-free fake without inheriting
    from the real Decoder (which crashes if libopus isn't installed).
    """

    def packet_get_nb_frames(self, data: bytes) -> int: ...

    def packet_get_samples_per_frame(self, data: bytes) -> int: ...

    def decode(self, data: bytes | None, *, fec: bool = ...) -> bytes: ...


log = logging.getLogger("rocketwatch.voice_summary.recorder")

# Discord audio: 48kHz, 16-bit signed, stereo
SAMPLE_RATE = OpusDecoder.SAMPLING_RATE  # 48000
CHANNELS = OpusDecoder.CHANNELS  # 2
SAMPLE_WIDTH = OpusDecoder.SAMPLE_SIZE // OpusDecoder.CHANNELS  # 2 bytes (16-bit)
FRAME_SIZE = CHANNELS * SAMPLE_WIDTH  # bytes per PCM frame
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
OPUS_FRAME_SAMPLES = OpusDecoder.SAMPLES_PER_FRAME  # 960 = 20ms at 48 kHz

SILENCE_DURATION = 1.0  # seconds of packet gap before splitting
MAX_SEGMENT_DURATION = 60.0  # seconds before forcing a split
# Up to this many missing 20ms frames are concealed (PLC + FEC) instead of
# zero-padded. Past ~100 ms, Opus PLC starts sounding robotic, so silence is
# the lesser evil.
MAX_CONCEAL_FRAMES = 5

SILENCE_DURATION_SAMPLES = int(SILENCE_DURATION * SAMPLE_RATE)
MAX_SEGMENT_DURATION_SAMPLES = int(MAX_SEGMENT_DURATION * SAMPLE_RATE)


def _opus_packet_samples(decoder: _OpusDecoderLike, data: bytes) -> int:
    """PCM samples (per channel) covered by an Opus packet, 0 if malformed."""
    try:
        nb_frames = decoder.packet_get_nb_frames(data)
        spf = decoder.packet_get_samples_per_frame(data)
    except Exception:
        return 0
    if nb_frames <= 0 or spf <= 0:
        return 0
    return nb_frames * spf


class _PendingSegment:
    """A segment being accumulated in memory.

    Holds raw Opus packets keyed by RTP timestamp. Sorting, decoding, and
    gap concealment all happen at finalize time so out-of-order arrivals
    land in their correct RTP slots and the decoder advances strictly in
    audio-time order (Opus PLC / FEC assume that).
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
        # rtp_timestamp -> (opus_data, n_samples_per_channel)
        self.packets: dict[int, tuple[bytes, int]] = {}
        self.stat_pkts = 0
        self.stat_drops = 0


class UserStream:
    """Buffers Opus packets for a single user, decoding to PCM at finalize time."""

    def __init__(
        self,
        user_id: int,
        out_dir: Path,
        recorder_start_time: float,
        on_segment_closed: Callable[[int, float, Path], None] | None = None,
        decoder_factory: Callable[[], _OpusDecoderLike] | None = None,
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
        # Single decoder per user, shared across segments so its internal
        # state evolves in audio-time order across the whole call.
        # OpusDecoder satisfies the Protocol at runtime, but mypy can't see
        # that — packet_get_samples_per_frame is a @classmethod on the real
        # class and Protocols can't match classmethods structurally.
        self._decoder: _OpusDecoderLike = (
            decoder_factory()
            if decoder_factory
            else cast(_OpusDecoderLike, OpusDecoder())
        )
        # Serializes sink-thread writes against main-thread close/flush.
        self._lock = threading.Lock()
        self._closed = False
        # Currently-active in-memory segment. Packets arrive here regardless
        # of order; it's sorted + decoded + written to disk on finalize.
        self._open: _PendingSegment | None = None
        # Furthest RTP position any already-finalized segment reached.
        # Packets whose entire audio extent ends at or before this point
        # belong to a closed WAV file we can no longer edit and are dropped.
        self._last_finalized_rtp_end: int | None = None

    def _rtp_to_call_time(self, rtp_ts: int) -> float:
        """Convert an RTP sample-clock timestamp to seconds from call start."""
        assert self._rtp_anchor is not None
        return self._wall_anchor + (rtp_ts - self._rtp_anchor) / SAMPLE_RATE

    def _anchor_rtp(self, rtp_ts: int) -> None:
        """Peg this stream's RTP clock to the current call time."""
        self._rtp_anchor = rtp_ts
        self._wall_anchor = time.monotonic() - self._recorder_start_time

    def write(self, opus_data: bytes, rtp_timestamp: int) -> None:
        with self._lock:
            if self._closed:
                return

            samples = _opus_packet_samples(self._decoder, opus_data)
            if samples <= 0:
                # Malformed packet — drop. We can't place it in time without
                # knowing its duration.
                return

            rtp_end = rtp_timestamp + samples

            # Late arrival for an already-finalized segment: the WAV is on
            # disk and we can't rewrite it.
            if (
                self._last_finalized_rtp_end is not None
                and rtp_end <= self._last_finalized_rtp_end
            ):
                if self._open is not None:
                    self._open.stat_drops += 1
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

            if self._open is None:
                self._open = _PendingSegment(rtp_timestamp, call_time)
            else:
                seg = self._open
                # Out-of-order arrival earlier than the segment's current
                # start: expand the segment's bounds. Real-world reorder is
                # tens of milliseconds, but the same code-path handles bulk
                # backfills cleanly too.
                if rtp_timestamp < seg.rtp_start:
                    seg.rtp_start = rtp_timestamp
                    seg.call_time = min(seg.call_time, call_time)
                over_max = rtp_timestamp - seg.rtp_start >= MAX_SEGMENT_DURATION_SAMPLES
                silence_gap = rtp_timestamp > seg.max_rtp_end + SILENCE_DURATION_SAMPLES
                if over_max or silence_gap:
                    self._finalize(seg)
                    self._open = _PendingSegment(rtp_timestamp, call_time)

            seg = self._open
            seg.packets[rtp_timestamp] = (opus_data, samples)
            seg.stat_pkts += 1
            if rtp_end > seg.max_rtp_end:
                seg.max_rtp_end = rtp_end

    def _conceal_gap(
        self,
        wav: wave.Wave_write,
        gap_samples: int,
        next_opus: bytes,
        stats: dict[str, int],
    ) -> None:
        """Fill a gap with PLC + FEC concealment or zero-pad, depending on size.

        ``next_opus`` is the packet that arrives after the gap; FEC uses it to
        reconstruct the immediately-preceding lost frame.
        """
        decoder = self._decoder
        gap_frames, gap_remainder = divmod(gap_samples, OPUS_FRAME_SAMPLES)

        if 0 < gap_frames <= MAX_CONCEAL_FRAMES:
            # PLC for the earlier lost frames, FEC for the one just before
            # next_opus (libopus falls back to PLC when no FEC payload is
            # present, so this is always safe to call).
            for _ in range(gap_frames - 1):
                try:
                    wav.writeframes(decoder.decode(None, fec=False))
                    stats["plc_frames"] += 1
                except Exception:
                    wav.writeframes(b"\x00" * OPUS_FRAME_SAMPLES * FRAME_SIZE)
                    stats["pad_samples"] += OPUS_FRAME_SAMPLES
            try:
                wav.writeframes(decoder.decode(next_opus, fec=True))
                stats["fec_frames"] += 1
            except Exception:
                try:
                    wav.writeframes(decoder.decode(None, fec=False))
                    stats["plc_frames"] += 1
                except Exception:
                    wav.writeframes(b"\x00" * OPUS_FRAME_SAMPLES * FRAME_SIZE)
                    stats["pad_samples"] += OPUS_FRAME_SAMPLES
        elif gap_frames > MAX_CONCEAL_FRAMES:
            wav.writeframes(b"\x00" * gap_frames * OPUS_FRAME_SAMPLES * FRAME_SIZE)
            stats["pad_samples"] += gap_frames * OPUS_FRAME_SAMPLES
            stats["pad_events"] += 1

        # Sub-frame remainder (RTP off-grid after a long silence): can't
        # conceal a fractional frame, just zero-fill it.
        if gap_remainder > 0:
            wav.writeframes(b"\x00" * gap_remainder * FRAME_SIZE)
            stats["pad_samples"] += gap_remainder

    def _finalize(self, seg: _PendingSegment) -> None:
        """Sort packets by RTP, decode in order with PLC/FEC, write the wav."""
        if not seg.packets:
            return

        sorted_packets = sorted(seg.packets.items())
        path = self._out_dir / f"{self.user_id}_{self._segment_index}.wav"
        self._segment_index += 1

        decoder = self._decoder
        stats = {
            "pcm_samples": 0,
            "pad_samples": 0,
            "pad_events": 0,
            "plc_frames": 0,
            "fec_frames": 0,
        }

        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(SAMPLE_WIDTH)
            wav.setframerate(SAMPLE_RATE)

            last_end_rtp = sorted_packets[0][0]

            for rtp, (opus, samples) in sorted_packets:
                if rtp < last_end_rtp:
                    # Overlap from a packet whose audio extent reached into
                    # another packet's slot. Skip the overlapping prefix.
                    try:
                        pcm = decoder.decode(opus, fec=False)
                    except Exception:
                        seg.stat_drops += 1
                        continue
                    skip = last_end_rtp - rtp
                    if skip >= samples:
                        seg.stat_drops += 1
                        continue
                    wav.writeframes(pcm[skip * FRAME_SIZE :])
                    stats["pcm_samples"] += samples - skip
                    last_end_rtp = rtp + samples
                    continue

                if rtp > last_end_rtp:
                    self._conceal_gap(wav, rtp - last_end_rtp, opus, stats)

                try:
                    pcm = decoder.decode(opus, fec=False)
                except Exception:
                    seg.stat_drops += 1
                    # Cursor must still advance, otherwise the next packet's
                    # gap math is off by this packet's worth of samples.
                    wav.writeframes(b"\x00" * samples * FRAME_SIZE)
                    stats["pad_samples"] += samples
                    last_end_rtp = rtp + samples
                    continue

                wav.writeframes(pcm)
                written = len(pcm) // FRAME_SIZE
                stats["pcm_samples"] += written
                last_end_rtp = rtp + written

        self._segments.append((seg.call_time, path))
        self._last_finalized_rtp_end = max(
            self._last_finalized_rtp_end or 0, seg.max_rtp_end
        )
        self._log_segment_stats(path, seg, stats)
        if self._on_segment_closed:
            self._on_segment_closed(self.user_id, seg.call_time, path)

    def _log_segment_stats(
        self, path: Path, seg: _PendingSegment, stats: dict[str, int]
    ) -> None:
        pcm_s = stats["pcm_samples"] / SAMPLE_RATE
        pad_s = stats["pad_samples"] / SAMPLE_RATE
        plc_s = stats["plc_frames"] * OPUS_FRAME_SAMPLES / SAMPLE_RATE
        fec_s = stats["fec_frames"] * OPUS_FRAME_SAMPLES / SAMPLE_RATE
        total_s = pcm_s + pad_s + plc_s + fec_s
        pct = pad_s / total_s * 100 if total_s else 0.0
        log.info(
            f"Segment {path.name}: pkts={seg.stat_pkts} "
            f"pcm={pcm_s:.2f}s pad={pad_s:.2f}s ({pct:.1f}%) "
            f"plc={plc_s:.2f}s fec={fec_s:.2f}s "
            f"pad_events={stats['pad_events']} drops={seg.stat_drops}"
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
    """Records per-user Opus packets from a Discord voice channel.

    Stashes raw Opus packets per user. Decoding (with PLC + FEC concealment
    for short gaps) happens at segment-finalize time inside each UserStream.
    """

    def __init__(
        self,
        out_dir: Path,
        start_time: float | None = None,
        on_segment_closed: Callable[[int, float, Path], None] | None = None,
        decoder_factory: Callable[[], _OpusDecoderLike] | None = None,
    ) -> None:
        self._out_dir = out_dir
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._streams: dict[int, UserStream] = {}
        self._start_time = start_time or time.monotonic()
        self._on_segment_closed = on_segment_closed
        self._decoder_factory = decoder_factory
        self._recording = True
        # Packets rejected before they reach a UserStream.
        self._drops_empty = 0

    def on_opus(self, user_id: int, opus_data: bytes, rtp_timestamp: int) -> None:
        """Receive an Opus packet and stash it on the user's stream.

        rtp_timestamp is the packet's RTP timestamp (48 kHz sample clock).
        It is used directly for intra-stream timing so network jitter doesn't
        show up as spurious silence or segment splits.
        """
        if not self._recording:
            return
        if not opus_data:
            self._drops_empty += 1
            return

        if user_id not in self._streams:
            log.info(f"Receiving audio from user {user_id}")
            self._streams[user_id] = UserStream(
                user_id,
                self._out_dir,
                self._start_time,
                self._on_segment_closed,
                decoder_factory=self._decoder_factory,
            )

        self._streams[user_id].write(opus_data, rtp_timestamp)

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
