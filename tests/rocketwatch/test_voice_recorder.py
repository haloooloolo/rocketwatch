"""Tests for the voice recorder's gap-concealment and packet-ordering behavior.

These exercise UserStream / CallRecorder against a scripted Opus decoder so they
can run without libopus on the host. The scripted decoder's PCM output is structured
just enough for tests to tell which input packet (or concealment kind)
produced which region of the WAV file.
"""

from __future__ import annotations

import time
import wave
from pathlib import Path

import pytest

from rocketwatch.plugins.voice_summary.recorder import (
    CHANNELS,
    FRAME_SIZE,
    OPUS_FRAME_SAMPLES,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    SILENCE_DURATION,
    CallRecorder,
)
from tests.lib.opus import ScriptedOpusDecoder


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
    def test_silence_above_threshold_starts_new_segment(self, tmp_path: Path) -> None:
        rec = CallRecorder(
            tmp_path, start_time=0.0, decoder_factory=ScriptedOpusDecoder
        )
        rec.on_opus(1, _packet(0x11), 0)
        rec.on_opus(1, _packet(0x22), OPUS_FRAME_SAMPLES)
        # Past SILENCE_DURATION, so a new segment opens instead of
        # zero-padding the hole.
        gap = int((SILENCE_DURATION + 1) * SAMPLE_RATE)
        rec.on_opus(1, _packet(0x33), 2 * OPUS_FRAME_SAMPLES + gap)
        rec.stop()

        segments = rec.get_user_segments()[1]
        assert len(segments) == 2
        # First segment ends at 2 frames; second begins fresh.
        first = _read_wav(segments[0][1])
        second = _read_wav(segments[1][1])
        assert len(first) == 2 * FRAME_BYTES
        assert len(second) == 1 * FRAME_BYTES

    def test_mid_sentence_pause_stays_in_one_segment(self, tmp_path: Path) -> None:
        rec = CallRecorder(
            tmp_path, start_time=0.0, decoder_factory=ScriptedOpusDecoder
        )
        rec.on_opus(1, _packet(0x11), 0)
        rec.on_opus(1, _packet(0x22), OPUS_FRAME_SAMPLES + int(1.5 * SAMPLE_RATE))
        rec.stop()

        assert len(rec.get_user_segments()[1]) == 1


def _speak(rec: CallRecorder, user_id: int, rtp_start: int, seconds: float) -> int:
    """Feed contiguous 20 ms packets; returns the RTP position after the last one."""
    n = int(seconds * SAMPLE_RATE) // OPUS_FRAME_SAMPLES
    for i in range(n):
        rec.on_opus(user_id, _packet(0x11), rtp_start + i * OPUS_FRAME_SAMPLES)
    return rtp_start + n * OPUS_FRAME_SAMPLES


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock)
    return clock


class TestSegmentPlacement:
    def test_rtp_stalled_through_silence_uses_arrival_time(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        # some clients don't advance RTP while silent; 30 s of real silence
        # must not pull the next utterance back onto earlier speech
        rec = CallRecorder(
            tmp_path, start_time=clock.now, decoder_factory=ScriptedOpusDecoder
        )
        rtp = _speak(rec, 1, 0, 1.0)
        clock.now += 31.0
        _speak(rec, 1, rtp + int((SILENCE_DURATION + 0.5) * SAMPLE_RATE), 1.0)
        rec.stop()

        offsets = [offset for offset, _ in rec.get_user_segments()[1]]
        assert offsets == [pytest.approx(0.0), pytest.approx(31.0)]

    def test_rtp_jump_ahead_uses_arrival_time(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        rec = CallRecorder(
            tmp_path, start_time=clock.now, decoder_factory=ScriptedOpusDecoder
        )
        rtp = _speak(rec, 1, 0, 1.0)
        clock.now += 4.0
        _speak(rec, 1, rtp + 60 * SAMPLE_RATE, 1.0)
        rec.stop()

        offsets = [offset for offset, _ in rec.get_user_segments()[1]]
        assert offsets == [pytest.approx(0.0), pytest.approx(4.0)]

    def test_max_duration_split_stays_contiguous(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        rec = CallRecorder(
            tmp_path, start_time=clock.now, decoder_factory=ScriptedOpusDecoder
        )
        # continuous speech arrives in real time, slightly jittered
        n = int(61 * SAMPLE_RATE) // OPUS_FRAME_SAMPLES
        for i in range(n):
            clock.now = 1000.0 + i * 0.02 + 0.015 * (i % 3)
            rec.on_opus(1, _packet(0x11), i * OPUS_FRAME_SAMPLES)
        rec.stop()

        (first_offset, first), (second_offset, _) = rec.get_user_segments()[1]
        first_len = len(_read_wav(first)) / FRAME_SIZE / SAMPLE_RATE
        assert second_offset == pytest.approx(first_offset + first_len)


class TestClockStallInsideSegment:
    def _feed(self, rec: CallRecorder, clock: Clock, arrivals: list[float]) -> Path:
        for i, arrival in enumerate(arrivals):
            clock.now = 1000.0 + arrival
            rec.on_opus(1, _packet(0x11), i * OPUS_FRAME_SAMPLES)
        rec.stop()
        [(_, wav)] = rec.get_user_segments()[1]
        return wav

    def test_short_pause_with_stalled_rtp_keeps_its_length(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        rec = CallRecorder(
            tmp_path, start_time=clock.now, decoder_factory=ScriptedOpusDecoder
        )
        # RTP runs on without a gap, but 1.5 s of real time pass mid-speech
        arrivals = [i * 0.02 for i in range(50)]
        arrivals += [1.5 + i * 0.02 for i in range(50, 150)]

        audio = _read_wav(self._feed(rec, clock, arrivals))

        assert len(audio) / FRAME_SIZE / SAMPLE_RATE == pytest.approx(4.5, abs=0.03)
        pause = audio[
            int(1.1 * SAMPLE_RATE) * FRAME_SIZE : int(2.4 * SAMPLE_RATE) * FRAME_SIZE
        ]
        assert pause == bytes(len(pause))

    def test_network_hiccup_is_not_a_stall(self, tmp_path: Path, clock: Clock) -> None:
        rec = CallRecorder(
            tmp_path, start_time=clock.now, decoder_factory=ScriptedOpusDecoder
        )
        # packets 40-54 are held up 300 ms, then burst in and catch up
        arrivals = [i * 0.02 for i in range(100)]
        for i in range(40, 55):
            arrivals[i] = 1.1 + (i - 40) * 0.001

        audio = _read_wav(self._feed(rec, clock, arrivals))

        assert len(audio) == 100 * FRAME_BYTES
        assert audio == bytes([0x11]) * len(audio)


class TestStreamRestart:
    def test_rejoin_with_lower_rtp_base_keeps_audio(self, tmp_path: Path) -> None:
        # A user who rejoins gets a new SSRC with a fresh random RTP base,
        # which may be far below where their previous stream ended.
        rec = CallRecorder(
            tmp_path, start_time=0.0, decoder_factory=ScriptedOpusDecoder
        )
        end = _speak(rec, 1, 3_000_000_000, 2)
        _speak(rec, 1, end + 10 * SAMPLE_RATE, 2)  # closes the first segment
        _speak(rec, 1, 1_000, 10)  # rejoined
        rec.stop()

        segments = rec.get_user_segments()[1]
        assert len(segments) == 3
        assert len(_read_wav(segments[2][1])) == 10 * SAMPLE_RATE * FRAME_SIZE

    def test_rtp_wraparound_keeps_audio(self, tmp_path: Path) -> None:
        rec = CallRecorder(
            tmp_path, start_time=0.0, decoder_factory=ScriptedOpusDecoder
        )
        end = _speak(rec, 1, 2**32 - 5 * SAMPLE_RATE, 4)
        _speak(rec, 1, end + 10 * SAMPLE_RATE - 2**32, 3)
        rec.stop()

        segments = rec.get_user_segments()[1]
        assert len(segments) == 2
        assert len(_read_wav(segments[1][1])) == 3 * SAMPLE_RATE * FRAME_SIZE


class TestTranscriptionCallback:
    def test_short_segments_are_recorded_but_not_transcribed(
        self, tmp_path: Path
    ) -> None:
        closed: list[Path] = []
        rec = CallRecorder(
            tmp_path,
            start_time=0.0,
            on_segment_closed=lambda _uid, _offset, path: closed.append(path),
            decoder_factory=ScriptedOpusDecoder,
        )
        end = _speak(rec, 1, 0, 0.5)
        _speak(rec, 1, end + 5 * SAMPLE_RATE, 3)
        rec.stop()

        segments = [path for _, path in rec.get_user_segments()[1]]
        assert len(segments) == 2
        assert closed == [segments[1]]


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
