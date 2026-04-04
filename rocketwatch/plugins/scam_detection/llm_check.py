import logging

from anthropic import AsyncAnthropic
from anthropic.types import TextBlock

from utils.config import cfg

log = logging.getLogger("rocketwatch.scam_detection.llm")

SYSTEM_PROMPT = """\
You are a scam detection system for a cryptocurrency Discord server.
Your job is to determine whether a message is attempting to manipulate or deceive users.

Focus on the author's INTENT. Flag messages that are trying to:
- Lure users away from public channels (into DMs, external platforms, profiles, etc.)
- Build false trust by impersonating authority (staff, support, admins)
- Create artificial urgency or fear to pressure users into action
- Deceive users through any other social engineering technique

Do NOT flag messages that:
- Mention DMs, profiles, or wallets in normal conversation without manipulative intent
- Offer genuine (even if clumsy) technical help
- Discuss problems, errors, or frustrations — even emotional ones
- Contain links shared in good faith

If the message is safe, respond with exactly: SAFE
If the message is a scam, respond with: SCAM: <brief reason>"""

USER_PROMPT_TEMPLATE = "Evaluate this Discord message:\n\n{content}"


class LLMScamChecker:
    def __init__(self) -> None:
        self._client: AsyncAnthropic | None = None
        if cfg.anthropic.api_key:
            self._client = AsyncAnthropic(api_key=cfg.anthropic.api_key)

    async def check(self, content: str) -> str | None:
        """Evaluate a message for social engineering using an LLM.

        Returns a reason string if the message is likely a scam, None otherwise.
        """
        if not self._client:
            return None

        response = await self._client.messages.create(
            model=cfg.anthropic.model,
            max_tokens=50,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": USER_PROMPT_TEMPLATE.format(content=content),
                },
            ],
        )

        block = response.content[0]
        assert isinstance(block, TextBlock)
        result = block.text.strip()
        log.debug(f"AI scam check result: {result}")

        if result.upper().startswith("SCAM"):
            reason = result.removeprefix("SCAM").lstrip(":").strip()
            if reason:
                return reason
        return None
