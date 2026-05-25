"""Tests for the voice recorder's gap-concealment and packet-ordering behavior.

These exercise UserStream / CallRecorder against a scripted Opus decoder so they
can run without libopus on the host. The scripted decoder's PCM output is structured
just enough for tests to tell which input packet (or concealment kind)
produced which region of the WAV file.
"""

from __future__ import annotations

import wave
from pathlib import Path

from rocketwatch.plugins.voice_summary.recorder import (
    CHANNELS,
    FRAME_SIZE,
    OPUS_FRAME_SAMPLES,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    CallRecorder,
)


class ScriptedOpusDecoder:
    """Stand-in for ``discord.opus.Decoder``.

    Each packet's "decoded" PCM is filled with the packet's first byte, so
    test code can identify which packet ended up where in the WAV. PLC and
    FEC produce their own distinct, recognizable patterns.
    """

    SAMPLING_RATE = 48000
    CHANNELS = 2
    SAMPLE_SIZE = 4  # bytes per stereo frame
    SAMPLES_PER_FRAME = 960
    FRAME_LENGTH = 20

    PLC_MARKER = b"\xfd"
    FEC_MARKER = b"\xfe"

    @staticmethod
    def packet_get_nb_frames(_data: bytes) -> int:
        return 1

    @staticmethod
    def packet_get_samples_per_frame(_data: bytes) -> int:
        return 960

    def decode(self, data: bytes | None, *, fec: bool = False) -> bytes:
        frame_bytes = self.SAMPLES_PER_FRAME * self.SAMPLE_SIZE
        if data is None:
            return self.PLC_MARKER * frame_bytes
        if fec:
            return self.FEC_MARKER * frame_bytes
        return bytes([data[0]]) * frame_bytes


def _read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == CHANNELS
        assert w.getframerate() == SAMPLE_RATE
        assert w.getsampwidth() == SAMPLE_WIDTH
        return w.readframes(w.getnframes())


def _packet(byte_id: int) -> bytes:
    return bytes([byte_id, 0])


FRAME_BYTES = OPUS_FRAME_SAMPLES * FRAME_SIZE


class TestContiguousPackets:
    def test_back_to_back_packets_pack_without_gaps(self, tmp_path: Path) -> None:
        # Without any gaps, every Opus frame should land in its 20ms slot
        # with no zero-padding inserted between them.
        rec = CallRecorder(
            tmp_path, start_time=0.0, decoder_factory=ScriptedOpusDecoder
        )
        for i in range(5):
            rec.on_opus(1, _packet(i + 1), 1000 + i * OPUS_FRAME_SAMPLES)
        rec.stop()

        segments = rec.get_user_segments()[1]
        assert len(segments) == 1
        data = _read_wav(segments[0][1])
        assert len(data) == 5 * FRAME_BYTES

        # Each packet's 20ms region is filled with its identifier byte, with no
        # PLC/FEC sentinels in between — confirming nothing concealment-related
        # ran when there were no gaps to conceal.
        for i in range(5):
            chunk = data[i * FRAME_BYTES : (i + 1) * FRAME_BYTES]
            assert chunk == bytes([i + 1]) * FRAME_BYTES


class TestOutOfOrderArrival:
    def test_packets_are_finalized_in_rtp_order(self, tmp_path: Path) -> None:
        # Spec: arrival order doesn't change the file — the WAV is laid out by
        # RTP timestamp. We verify this by producing two recordings (in-order
        # vs reversed) and asserting their bytes match.
        in_order = tmp_path / "in"
        reversed_ = tmp_path / "rev"
        in_order.mkdir()
        reversed_.mkdir()

        rec_a = CallRecorder(
            in_order, start_time=0.0, decoder_factory=ScriptedOpusDecoder
        )
        rec_b = CallRecorder(
            reversed_, start_time=0.0, decoder_factory=ScriptedOpusDecoder
        )

        rtps = [1000 + i * OPUS_FRAME_SAMPLES for i in range(5)]
        for i, rtp in enumerate(rtps):
            rec_a.on_opus(1, _packet(i + 1), rtp)
        for i, rtp in reversed(list(enumerate(rtps))):
            rec_b.on_opus(1, _packet(i + 1), rtp)

        rec_a.stop()
        rec_b.stop()

        a = _read_wav(rec_a.get_user_segments()[1][0][1])
        b = _read_wav(rec_b.get_user_segments()[1][0][1])
        assert a == b


class TestShortGapConcealment:
    def test_one_frame_gap_uses_fec_recovery(self, tmp_path: Path) -> None:
        # A 1-frame gap: PLC for 0 frames, FEC-recover the one missing frame
        # from the next packet. No zero-padding should appear.
        rec = CallRecorder(
            tmp_path, start_time=0.0, decoder_factory=ScriptedOpusDecoder
        )
        rec.on_opus(1, _packet(0x11), 0)
        # Skip one frame slot (at rtp 960), packet resumes at 1920.
        rec.on_opus(1, _packet(0x22), 2 * OPUS_FRAME_SAMPLES)
        rec.stop()

        data = _read_wav(rec.get_user_segments()[1][0][1])
        assert len(data) == 3 * FRAME_BYTES

        # Middle frame is the FEC-recovered one, not zero-padding.
        gap_region = data[FRAME_BYTES : 2 * FRAME_BYTES]
        assert gap_region == ScriptedOpusDecoder.FEC_MARKER * FRAME_BYTES

    def test_multi_frame_gap_uses_plc_then_fec(self, tmp_path: Path) -> None:
        # 4-frame gap: PLC fills the first 3, FEC fills the last.
        rec = CallRecorder(
            tmp_path, start_time=0.0, decoder_factory=ScriptedOpusDecoder
        )
        rec.on_opus(1, _packet(0x11), 0)
        rec.on_opus(1, _packet(0x22), 5 * OPUS_FRAME_SAMPLES)
        rec.stop()

        data = _read_wav(rec.get_user_segments()[1][0][1])
        assert len(data) == 6 * FRAME_BYTES

        # Frames 1..3 are PLC, frame 4 is FEC, frame 5 is the actual packet.
        for plc_idx in (1, 2, 3):
            chunk = data[plc_idx * FRAME_BYTES : (plc_idx + 1) * FRAME_BYTES]
            assert chunk == ScriptedOpusDecoder.PLC_MARKER * FRAME_BYTES
        fec_chunk = data[4 * FRAME_BYTES : 5 * FRAME_BYTES]
        assert fec_chunk == ScriptedOpusDecoder.FEC_MARKER * FRAME_BYTES


class TestLongGapZeroPadding:
    def test_gap_above_threshold_is_zero_padded(self, tmp_path: Path) -> None:
        # Past MAX_CONCEAL_FRAMES (= 5), concealment sounds robotic; the
        # recorder should fall back to zero-fill.
        rec = CallRecorder(
            tmp_path, start_time=0.0, decoder_factory=ScriptedOpusDecoder
        )
        rec.on_opus(1, _packet(0x11), 0)
        # 10-frame gap (= 200 ms) — well past the conceal threshold but
        # still under the SILENCE_DURATION segment-split threshold.
        rec.on_opus(1, _packet(0x22), 11 * OPUS_FRAME_SAMPLES)
        rec.stop()

        segments = rec.get_user_segments()[1]
        # Stays in one segment because the gap is < SILENCE_DURATION.
        assert len(segments) == 1

        data = _read_wav(segments[0][1])
        assert len(data) == 12 * FRAME_BYTES

        # Frames 1..10 must all be exactly zero (zero-pad, not concealment).
        gap_region = data[FRAME_BYTES : 11 * FRAME_BYTES]
        assert gap_region == b"\x00" * len(gap_region)


class TestSegmentSplitOnLongSilence:
    def test_silence_above_one_second_starts_new_segment(self, tmp_path: Path) -> None:
        rec = CallRecorder(
            tmp_path, start_time=0.0, decoder_factory=ScriptedOpusDecoder
        )
        rec.on_opus(1, _packet(0x11), 0)
        rec.on_opus(1, _packet(0x22), OPUS_FRAME_SAMPLES)
        # 2 second silence — past SILENCE_DURATION (1 s) so a new segment
        # opens instead of zero-padding a 2 s hole.
        rec.on_opus(1, _packet(0x33), OPUS_FRAME_SAMPLES + 2 * SAMPLE_RATE)
        rec.stop()

        segments = rec.get_user_segments()[1]
        assert len(segments) == 2
        # First segment ends at 2 frames; second begins fresh.
        first = _read_wav(segments[0][1])
        second = _read_wav(segments[1][1])
        assert len(first) == 2 * FRAME_BYTES
        assert len(second) == 1 * FRAME_BYTES


class TestOverlappingPackets:
    def test_overlap_is_trimmed_not_appended(self, tmp_path: Path) -> None:
        # If two packets claim overlapping RTP ranges (rare; happens with
        # retransmits or buggy senders), only the non-overlapping suffix
        # of the second packet should be written. With a half-frame overlap,
        # the file ends up 1.5 frames long, not the naive 2 frames you'd get
        # from blind concatenation.
        rec = CallRecorder(
            tmp_path, start_time=0.0, decoder_factory=ScriptedOpusDecoder
        )
        rec.on_opus(1, _packet(0x11), 0)
        # Second packet starts at rtp = 480 (= half a frame into the first).
        rec.on_opus(1, _packet(0x22), OPUS_FRAME_SAMPLES // 2)
        rec.stop()

        data = _read_wav(rec.get_user_segments()[1][0][1])
        # First packet: 1 full frame. Second packet contributes its second
        # half only — 480 samples — because the first 480 overlap.
        expected = FRAME_BYTES + (OPUS_FRAME_SAMPLES // 2) * FRAME_SIZE
        assert len(data) == expected
        # The first frame is the first packet's payload, untouched.
        assert data[:FRAME_BYTES] == bytes([0x11]) * FRAME_BYTES
