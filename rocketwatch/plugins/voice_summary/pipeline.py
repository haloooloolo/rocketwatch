import io
import logging
import re
from pathlib import Path

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from rocketwatch.utils.config import STTConfig
from rocketwatch.utils.llm import LLMProvider

log = logging.getLogger("rocketwatch.voice_summary.pipeline")

SUMMARY_CHAR_LIMIT = 3800

SUMMARIZE_SYSTEM_PROMPT = """\
You are summarizing a Rocket Pool community call transcript for Discord server members \
who missed the call. Produce a structured summary with the following sections:

- **Topics Discussed**: Brief overview of each subject covered
- **Decisions & Outcomes**: Conclusions reached, proposals approved or rejected
- **Action Items**: Tasks assigned or next steps, attributed to specific people if mentioned
- **Open Questions**: Unresolved topics or debates that need follow-up

Attribute key statements to speakers by name when relevant. \
When referring to a participant listed in the roster, write their handle token \
(e.g. `@1`, `@2`) exactly as shown in the roster instead of their display name. \
Only use handles that appear in the roster; do not invent new ones. \
Omit any section that has no content. Use bullet points. \
Give equal attention to all parts of the transcript, regardless of when they occurred.

HARD LIMIT: the summary must be at most 500 words; aim for 300-450. Prefer brevity \
over exhaustiveness: collapse related points into single bullets, drop minor tangents, \
and tighten wording. One concise sentence per bullet — no sub-bullets, no long paragraphs.

If the transcript contains no substantive discussion worth summarizing \
(e.g. only greetings, idle chatter, silence, or people joining/leaving), \
set has_content to false and leave summary empty.

The `summary` field must be a plain markdown string — bold section headings \
followed by bullet points. Do NOT emit JSON, arrays, or nested objects inside it."""

SHORTEN_SYSTEM_PROMPT = """\
You will be given a structured summary that is too long for its destination. \
Rewrite it to be shorter while preserving the same section structure and the most \
important facts, decisions, and action items. Drop minor details, merge related \
bullets, and tighten wording. Keep bullet points and section headers. \
Preserve any handle tokens of the form `@1`, `@2`, etc. exactly as they appear. \
Return only the shortened summary, nothing else."""


class SummaryResult(BaseModel):
    has_content: bool = Field(
        description="Whether the transcript contains substantive discussion worth posting."
    )
    summary: str = Field(
        default="",
        description="The structured summary of the transcript.",
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

    async def transcribe_wav(self, wav_path: Path) -> str:
        """Transcribe a single WAV file."""
        client = AsyncOpenAI(api_key=self._stt.api_key)

        buf = io.BytesIO(wav_path.read_bytes())
        buf.name = wav_path.name

        response = await client.audio.transcriptions.create(
            model=self._stt.model,
            language="en",
            file=buf,
            response_format="json",
        )
        return response.text.strip()

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

    async def summarize(self, transcript: str, usernames: dict[int, str]) -> str | None:
        """Summarize a transcript using the configured LLM.

        Returns None if the LLM determines there is no substantive content.
        """
        # Assign a small positional handle to each participant so the model
        # can reference them with short tokens (@1, @2, ...) rather than
        # copying long Discord IDs verbatim.
        handle_to_id = {i + 1: uid for i, uid in enumerate(usernames)}
        roster = "\n".join(
            f"- @{i} refers to {usernames[uid]}" for i, uid in handle_to_id.items()
        )
        user_message = (
            "Participant roster — when referring to any of these people in the "
            "summary, write the handle token instead of their name:\n"
            f"{roster}\n\n"
            f"Summarize this community call transcript:\n\n{transcript}"
        )
        result = await self._llm.complete_structured(
            SUMMARIZE_SYSTEM_PROMPT,
            user_message,
            SummaryResult,
            max_tokens=2048,
        )
        if not result.has_content:
            return None

        summary = result.summary
        for attempt in range(2):
            if len(summary) <= SUMMARY_CHAR_LIMIT:
                break
            log.info(
                f"Summary is {len(summary)} chars, shortening (attempt {attempt + 1})"
            )
            summary = await self._llm.complete(
                SHORTEN_SYSTEM_PROMPT,
                f"Shorten this summary to under {SUMMARY_CHAR_LIMIT} characters "
                f"(currently {len(summary)}):\n\n{summary}",
                max_tokens=2048,
            )

        return self._expand_handles(summary, handle_to_id, usernames)

    @staticmethod
    def _expand_handles(
        text: str, handle_to_id: dict[int, int], usernames: dict[int, str]
    ) -> str:
        """Replace @N handles with real Discord mentions; unknown handles get the name or are dropped."""

        def repl(match: re.Match[str]) -> str:
            handle = int(match.group(1))
            uid = handle_to_id.get(handle)
            if uid is not None:
                return f"<@{uid}>"
            return "someone"

        return re.sub(r"@(\d+)", repl, text)

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
                if text := await self.transcribe_wav(wav_path):
                    user_segs.append((offset, text))
            segments[user_id] = user_segs

        transcript = self.format_transcript(segments, usernames)
        summary = await self.summarize(transcript, usernames)
        return transcript, summary
