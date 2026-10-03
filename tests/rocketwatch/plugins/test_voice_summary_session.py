from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
import soundfile as sf
from discord.ext.voice_recv import BasicSink, VoiceRecvClient

from rocketwatch.plugins.voice_summary import recorder as recorder_mod
from rocketwatch.plugins.voice_summary import session as session_mod
from rocketwatch.plugins.voice_summary.pipeline import (
    SummaryResult,
    TranscriptionPipeline,
)
from rocketwatch.plugins.voice_summary.recorder import (
    OPUS_FRAME_SAMPLES,
    SAMPLE_RATE,
    CallRecorder,
)
from rocketwatch.plugins.voice_summary.session import CallSession
from rocketwatch.utils.config import STTConfig
from tests.lib.discord_harness import make_bot
from tests.lib.opus import ScriptedOpusDecoder

ALICE, BOB = 111, 222
NAMES = {ALICE: "Alice", BOB: "Bob"}
TEXT = {ALICE: "gm", BOB: "gn"}


CALL_START = 1000.0


class Clock:
    now = CALL_START

    def monotonic(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Clock:
    clock = Clock()
    # recorder-only: the event loop reads time.monotonic too
    monkeypatch.setattr(
        recorder_mod, "time", SimpleNamespace(monotonic=clock.monotonic)
    )
    monkeypatch.setattr(session_mod, "TRANSCRIPTIONS_DIR", tmp_path)
    monkeypatch.setattr(
        session_mod,
        "CallRecorder",
        partial(CallRecorder, decoder_factory=ScriptedOpusDecoder),
    )
    return clock


@pytest.fixture
def llm() -> MagicMock:
    llm = MagicMock()
    llm.complete_structured = AsyncMock(
        return_value=SummaryResult(has_content=True, summary="Call recap")
    )
    return llm


@pytest.fixture
def transcribe() -> AsyncMock:
    async def by_speaker(wav_path: Path) -> str:
        return TEXT[int(wav_path.name.split("_")[0])]

    return AsyncMock(side_effect=by_speaker)


@pytest.fixture
def bot() -> MagicMock:
    bot = make_bot()
    bot.get_or_fetch_member = AsyncMock(
        side_effect=lambda _guild, uid: MagicMock(display_name=NAMES[uid])
    )
    return bot


@pytest.fixture
def session(llm: MagicMock, transcribe: AsyncMock, bot: MagicMock) -> CallSession:
    pipeline = TranscriptionPipeline(
        STTConfig(provider="openai", model="gpt-transcribe"), llm
    )
    pipeline.transcribe_wav = transcribe  # type: ignore[method-assign]
    return CallSession(pipeline, bot)


@pytest.fixture
def vc() -> MagicMock:
    vc = MagicMock(spec=VoiceRecvClient)
    vc.is_connected.return_value = True
    vc.is_listening.return_value = False
    vc.disconnect = AsyncMock()
    vc._connection = MagicMock(dave_session=None)
    vc.channel = MagicMock()
    return vc


def _sink(vc: MagicMock) -> BasicSink:
    sink = vc.listen.call_args.args[0]
    assert isinstance(sink, BasicSink)
    return sink


def _speak(vc: MagicMock, clock: Clock, user_id: int, seconds: float) -> None:
    """Speak from the call's start, packets arriving in real time."""
    sink = _sink(vc)
    user = MagicMock(id=user_id)
    for i in range(int(seconds * SAMPLE_RATE / OPUS_FRAME_SAMPLES)):
        clock.now = CALL_START + i * 0.02
        packet = MagicMock(decrypted_data=bytes([7, 0]), timestamp=i * 960)
        sink.write(user, MagicMock(packet=packet))


class TestCall:
    async def test_produces_transcript_summary_and_audio(
        self, session: CallSession, vc: MagicMock, clock: Clock
    ) -> None:
        await session.start(vc)
        _speak(vc, clock, ALICE, 1.2)
        _speak(vc, clock, BOB, 1.2)

        result = await session.finalize()

        assert result is not None
        assert "Alice: gm" in result.transcript
        assert "Bob: gn" in result.transcript
        assert result.summary == "Call recap"
        audio, rate = sf.read(str(result.audio_path))
        assert rate == SAMPLE_RATE
        assert len(audio) / SAMPLE_RATE == pytest.approx(1.2, abs=0.1)
        vc.disconnect.assert_awaited_once()

    async def test_silent_call_is_discarded(
        self,
        session: CallSession,
        vc: MagicMock,
        llm: MagicMock,
        transcribe: AsyncMock,
    ) -> None:
        await session.start(vc)

        assert await session.finalize() is None
        transcribe.assert_not_awaited()
        llm.complete_structured.assert_not_awaited()

    async def test_call_without_substance_is_discarded(
        self, session: CallSession, vc: MagicMock, clock: Clock, llm: MagicMock
    ) -> None:
        llm.complete_structured.return_value = SummaryResult(has_content=False)
        await session.start(vc)
        _speak(vc, clock, ALICE, 1.2)

        assert await session.finalize() is None

    async def test_failed_segment_leaves_rest_of_transcript(
        self,
        session: CallSession,
        vc: MagicMock,
        clock: Clock,
        transcribe: AsyncMock,
    ) -> None:
        async def bob_fails(wav_path: Path) -> str:
            if wav_path.name.startswith(str(BOB)):
                raise RuntimeError("stt down")
            return "gm"

        transcribe.side_effect = bob_fails
        await session.start(vc)
        _speak(vc, clock, ALICE, 1.2)
        _speak(vc, clock, BOB, 1.2)

        result = await session.finalize()

        assert result is not None
        assert result.transcript == "[0:00] Alice: gm"

    async def test_speaker_names_fall_back_to_user_then_id(
        self, session: CallSession, vc: MagicMock, clock: Clock, bot: MagicMock
    ) -> None:
        bot.get_or_fetch_member = AsyncMock(side_effect=LookupError)

        def only_alice(uid: int) -> MagicMock:
            if uid != ALICE:
                raise LookupError
            return MagicMock(display_name="alice.eth")

        bot.get_or_fetch_user = AsyncMock(side_effect=only_alice)
        await session.start(vc)
        _speak(vc, clock, ALICE, 1.2)
        _speak(vc, clock, BOB, 1.2)

        result = await session.finalize()

        assert result is not None
        assert "alice.eth: gm" in result.transcript
        assert f"{BOB}: gn" in result.transcript

    async def test_undecryptable_dave_audio_is_dropped(
        self, session: CallSession, vc: MagicMock, clock: Clock
    ) -> None:
        dave = vc._connection.dave_session = MagicMock()
        dave.can_passthrough.return_value = False
        dave.decrypt.side_effect = ValueError("no key")
        await session.start(vc)
        _speak(vc, clock, ALICE, 1.2)

        assert await session.finalize() is None

    async def test_dave_audio_is_decrypted_before_recording(
        self, session: CallSession, vc: MagicMock, clock: Clock
    ) -> None:
        dave = vc._connection.dave_session = MagicMock()
        dave.can_passthrough.return_value = False
        dave.decrypt.side_effect = lambda _uid, _kind, data: data
        await session.start(vc)
        _speak(vc, clock, ALICE, 1.2)

        result = await session.finalize()

        assert result is not None
        assert dave.decrypt.call_count > 0


class TestMixAudio:
    def test_segments_land_at_their_offsets(
        self, session: CallSession, tmp_path: Path
    ) -> None:
        tone = (np.sin(np.arange(SAMPLE_RATE // 2) / 10) * 10000).astype(np.int16)
        for name in ("a.wav", "b.wav"):
            sf.write(str(tmp_path / name), tone, SAMPLE_RATE)

        out = session.mix_audio(
            {ALICE: [(0.0, tmp_path / "a.wav")], BOB: [(1.0, tmp_path / "b.wav")]}
        )

        audio, _ = sf.read(str(out))
        assert len(audio) / SAMPLE_RATE == pytest.approx(1.5, abs=0.1)
        second = SAMPLE_RATE
        assert np.abs(audio[int(0.1 * second) : int(0.4 * second)]).max() > 0.1
        assert np.abs(audio[int(0.6 * second) : int(0.9 * second)]).max() < 0.01
        assert np.abs(audio[int(1.1 * second) : int(1.4 * second)]).max() > 0.1
