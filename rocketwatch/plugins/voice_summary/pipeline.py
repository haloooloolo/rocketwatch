import io
import logging
from pathlib import Path

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from rocketwatch.utils.config import STTConfig
from rocketwatch.utils.llm import LLMProvider

log = logging.getLogger("rocketwatch.voice_summary.pipeline")

SUMMARIZE_SYSTEM_PROMPT = """\
You are summarizing a Rocket Pool community call transcript for Discord server members \
who missed the call. Produce a structured summary with the following sections:

- **Topics Discussed**: Brief overview of each subject covered
- **Decisions & Outcomes**: Conclusions reached, proposals approved or rejected
- **Action Items**: Tasks assigned or next steps, attributed to specific people if mentioned
- **Open Questions**: Unresolved topics or debates that need follow-up

Attribute key statements to speakers by name when relevant. \
Omit any section that has no content. Use bullet points and keep the summary concise. \
Give equal attention to all parts of the transcript, regardless of when they occurred.

If the transcript contains no substantive discussion worth summarizing \
(e.g. only greetings, idle chatter, silence, or people joining/leaving), \
set has_content to false and leave summary empty."""


class SummaryResult(BaseModel):
    has_content: bool = Field(
        description="Whether the transcript contains substantive discussion worth posting."
    )
    summary: str = Field(
        default="",
        description="The structured summary of the transcript. Always provide a summary.",
    )


class _Segment:
    """A timestamped piece of transcript from a single speaker."""

    __slots__ = ("speaker", "start", "text")

    def __init__(self, start: float, speaker: str, text: str) -> None:
        self.start = start
        self.speaker = speaker
        self.text = text


class TranscriptionPipeline:
    def __init__(
        self,
        stt_config: STTConfig,
        llm_provider: LLMProvider,
    ) -> None:
        self._stt = stt_config
        self._llm = llm_provider

    async def transcribe_wav(
        self,
        wav_path: Path,
    ) -> str | None:
        """Transcribe a single WAV file. Returns the text, or None if empty."""
        client = AsyncOpenAI(api_key=self._stt.api_key)

        buf = io.BytesIO(wav_path.read_bytes())
        buf.name = wav_path.name

        response = await client.audio.transcriptions.create(
            model=self._stt.model,
            file=buf,
            response_format="json",
        )

        text = response.text
        if text and text.strip():
            return text.strip()
        return None

    @staticmethod
    def format_transcript(
        segments: dict[int, list[tuple[float, str]]],
        usernames: dict[int, str],
    ) -> str:
        """Sort all segments by timestamp and format as a transcript string.

        Args:
            segments: user_id -> [(offset_seconds, text), ...]
            usernames: user_id -> display name
        """
        entries: list[_Segment] = []
        for user_id, user_segments in segments.items():
            speaker = usernames.get(user_id, f"User {user_id}")
            for start, text in user_segments:
                entries.append(_Segment(start=start, speaker=speaker, text=text))

        entries.sort(key=lambda s: s.start)

        lines: list[str] = []
        for seg in entries:
            minutes = int(seg.start) // 60
            seconds = int(seg.start) % 60
            lines.append(f"[{minutes}:{seconds:02d}] {seg.speaker}: {seg.text}")

        return "\n".join(lines)

    async def summarize(self, transcript: str) -> str | None:
        """Summarize a transcript using the configured LLM.

        Returns None if the LLM determines there is no substantive content.
        """
        result = await self._llm.complete_structured(
            SUMMARIZE_SYSTEM_PROMPT,
            f"Summarize this community call transcript:\n\n{transcript}",
            SummaryResult,
            max_tokens=2048,
        )
        if not result.has_content:
            return None
        return result.summary

    async def process_users(
        self,
        user_segments: dict[int, list[tuple[float, Path]]],
        usernames: dict[int, str],
    ) -> tuple[str, str | None]:
        """Full pipeline with speaker labels. Returns (transcript, summary).

        Summary is None if the LLM determines there is no substantive content.
        """
        segments: dict[int, list[tuple[float, str]]] = {}
        for user_id, wav_list in user_segments.items():
            user_segs: list[tuple[float, str]] = []
            for offset, wav_path in wav_list:
                text = await self.transcribe_wav(wav_path)
                if text:
                    user_segs.append((offset, text))
            segments[user_id] = user_segs

        transcript = self.format_transcript(segments, usernames)
        summary = await self.summarize(transcript)
        return transcript, summary
