import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import omit

from rocketwatch.plugins.voice_summary import pipeline as pipeline_mod
from rocketwatch.plugins.voice_summary.pipeline import (
    MAX_CONCURRENT_TRANSCRIPTIONS,
    SUMMARY_CHAR_LIMIT,
    SummaryResult,
    TranscriptionPipeline,
)
from rocketwatch.utils.config import STTConfig


@pytest.fixture
def transcribe(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    create = AsyncMock(return_value=MagicMock(text="  hello rETH  "))
    client = MagicMock()
    client.audio.transcriptions.create = create
    monkeypatch.setattr(pipeline_mod, "AsyncOpenAI", MagicMock(return_value=client))
    return create


@pytest.fixture
def wav(tmp_path: Path) -> Path:
    path = tmp_path / "clip.wav"
    path.write_bytes(b"RIFF")
    return path


class TestTranscribeWav:
    async def test_sends_configured_keywords(
        self, transcribe: AsyncMock, wav: Path
    ) -> None:
        stt = STTConfig(
            provider="openai", model="gpt-transcribe", keywords=["rETH", "minipool"]
        )
        text = await TranscriptionPipeline(stt, MagicMock()).transcribe_wav(wav)

        assert text == "hello rETH"
        kwargs = transcribe.call_args.kwargs
        assert kwargs["model"] == "gpt-transcribe"
        assert list(kwargs["keywords"]) == ["rETH", "minipool"]

    async def test_omits_keywords_when_unset(
        self, transcribe: AsyncMock, wav: Path
    ) -> None:
        # keywords are only documented as supported by gpt-transcribe
        stt = STTConfig(provider="openai", model="gpt-4o-transcribe")
        await TranscriptionPipeline(stt, MagicMock()).transcribe_wav(wav)

        assert transcribe.call_args.kwargs.get("keywords", omit) is omit

    async def test_limits_concurrent_requests(
        self, transcribe: AsyncMock, wav: Path
    ) -> None:
        in_flight = peak = 0
        release = asyncio.Event()

        async def slow_create(**_: object) -> MagicMock:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await release.wait()
            in_flight -= 1
            return MagicMock(text="ok")

        transcribe.side_effect = slow_create
        pipeline = TranscriptionPipeline(STT, MagicMock())
        tasks = [asyncio.create_task(pipeline.transcribe_wav(wav)) for _ in range(10)]
        await asyncio.sleep(0.01)
        assert peak == MAX_CONCURRENT_TRANSCRIPTIONS

        release.set()
        assert await asyncio.gather(*tasks) == ["ok"] * 10

    async def test_reuses_one_client(
        self, monkeypatch: pytest.MonkeyPatch, transcribe: AsyncMock, wav: Path
    ) -> None:
        client = MagicMock()
        client.audio.transcriptions.create = transcribe
        ctor = MagicMock(return_value=client)
        monkeypatch.setattr(pipeline_mod, "AsyncOpenAI", ctor)
        pipeline = TranscriptionPipeline(STT, MagicMock())
        for _ in range(3):
            await pipeline.transcribe_wav(wav)

        assert ctor.call_count == 1


STT = STTConfig(provider="openai", model="gpt-transcribe")
USERS = {111: "Alice", 222: "Bob"}


def _llm(summary: str = "", *, has_content: bool = True) -> MagicMock:
    llm = MagicMock()
    llm.complete_structured = AsyncMock(
        return_value=SummaryResult(has_content=has_content, summary=summary)
    )
    llm.complete = AsyncMock()
    return llm


class TestFormatTranscript:
    def test_interleaves_speakers_chronologically(self) -> None:
        transcript = TranscriptionPipeline.format_transcript(
            {111: [(0.0, "hi"), (75.9, "bye")], 222: [(3.2, "hello")]}, USERS
        )

        assert transcript.splitlines() == [
            "[0:00] Alice: hi",
            "[0:03] Bob: hello",
            "[1:15] Alice: bye",
        ]

    def test_unresolved_speaker_falls_back_to_id(self) -> None:
        transcript = TranscriptionPipeline.format_transcript({333: [(0.0, "hey")]}, {})

        assert transcript == "[0:00] User 333: hey"


class TestSummarize:
    async def test_no_substantive_content_returns_none(self) -> None:
        llm = _llm(has_content=False)

        assert await TranscriptionPipeline(STT, llm).summarize("t", USERS) is None

    async def test_roster_handles_become_mentions(self) -> None:
        llm = _llm("@1 proposed a vote, @2 agreed, @9 objected")

        summary = await TranscriptionPipeline(STT, llm).summarize("t", USERS)

        assert summary == "<@111> proposed a vote, <@222> agreed, someone objected"
        prompt = llm.complete_structured.call_args.args[1]
        assert "@1 refers to Alice" in prompt
        assert "@2 refers to Bob" in prompt

    async def test_short_summary_is_not_rewritten(self) -> None:
        llm = _llm("fine")

        await TranscriptionPipeline(STT, llm).summarize("t", USERS)

        llm.complete.assert_not_awaited()

    async def test_overlong_summary_is_shortened(self) -> None:
        llm = _llm("x" * (SUMMARY_CHAR_LIMIT + 1))
        llm.complete.return_value = "short @1"

        summary = await TranscriptionPipeline(STT, llm).summarize("t", USERS)

        assert summary == "short <@111>"
        llm.complete.assert_awaited_once()

    async def test_shortening_gives_up_after_two_attempts(self) -> None:
        too_long = "x" * (SUMMARY_CHAR_LIMIT + 1)
        llm = _llm(too_long)
        llm.complete.return_value = too_long

        summary = await TranscriptionPipeline(STT, llm).summarize("t", USERS)

        assert summary == too_long
        assert llm.complete.await_count == 2


class TestProcessUsers:
    async def test_transcribes_and_summarizes(
        self, transcribe: AsyncMock, tmp_path: Path
    ) -> None:
        transcribe.side_effect = [MagicMock(text="gm"), MagicMock(text="  ")]
        llm = _llm("summary")
        clips = {111: [(0.0, tmp_path / "a.wav")], 222: [(2.0, tmp_path / "b.wav")]}
        for clip in (tmp_path / "a.wav", tmp_path / "b.wav"):
            clip.write_bytes(b"RIFF")

        transcript, summary = await TranscriptionPipeline(STT, llm).process_users(
            clips, USERS
        )

        # silent clips don't produce empty transcript lines
        assert transcript == "[0:00] Alice: gm"
        assert summary == "summary"
