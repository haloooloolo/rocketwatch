from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import omit

from rocketwatch.plugins.voice_summary import pipeline as pipeline_mod
from rocketwatch.plugins.voice_summary.pipeline import TranscriptionPipeline
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
