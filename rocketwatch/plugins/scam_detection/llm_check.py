import json
import logging

import humanize
from discord import Member, Message
from discord.utils import utcnow
from pydantic import BaseModel, Field

from rocketwatch.plugins.scam_detection.common import message_to_dict
from rocketwatch.utils.config import cfg
from rocketwatch.utils.llm import ImageInput, LLMProvider, create_provider

log = logging.getLogger("rocketwatch.scam_detection.llm")

MAX_OUTPUT_TOKENS = 200
MAX_IMAGES = 5
MAX_IMAGE_BYTES = 4 * 1024 * 1024

SYSTEM_PROMPT = """\
You are a scam detection system for a cryptocurrency Discord server.
Your job is to determine whether a message is attempting to manipulate or deceive users.

Focus on the author's INTENT. Flag messages that are trying to:
- Lure users away from public channels (into DMs, external platforms, profiles, etc.)
- Build false trust by impersonating authority (staff, support, admins)
- Create artificial urgency or fear to pressure users into action
- Deceive users through any other social engineering technique

If image attachments are included, evaluate them as part of the message. Images may contain \
QR codes linking to phishing sites, fake support screenshots, fake wallet/exchange interfaces, \
impersonations of project branding, or instructions overlaid on otherwise innocuous pictures.

You will be given context about the user: how long they have been in the server and the number of previous \
messages they have sent in the server. Brand-new users with few or no messages who jump straight \
into offering help or directing others are highly suspicious.

Do NOT flag messages that:
- Mention DMs, profiles, or wallets in normal conversation without manipulative intent
- Offer genuine (even if clumsy) technical help
- Discuss problems, errors, or frustrations — even emotional ones
- Contain links shared in good faith
- Inquire about partnering, collaborating or integrating with the protocol

Examples:

"I've sent you a guide, kindly check. I had a similar issue but it was resolved"
-> is_scam=true, reason="Steering to DMs"

"Apologies for the inconvenience. For any inquiries or support, please use the official link in my bio to reach the technical team and moderators."
-> is_scam=true, reason="Profile link redirection"

"You need assistance mate?"
-> is_scam=true, reason="Unsolicited help offer"

"This support is useless, where do I actually get help?"
-> is_scam=false, reason="Genuine frustration"

"Can someone explain how the minipool bond reduction works?"
-> is_scam=false, reason="Technical question"

"My node has been offline for 2 days and I keep getting penalties, is there something wrong with the network?"
-> is_scam=false, reason="Asking for help"

"Is rocket pool open for a partnership with an ICO Platform? I'm from Legion"
-> is_scam=false, reason="Partnership proposal"
"""


class ScamCheckResult(BaseModel):
    is_scam: bool = Field(description="Whether the message is a scam attempt.")
    reason: str = Field(
        default="",
        description="Brief reason for the verdict (5 words max). Always provide a reason.",
    )


USER_PROMPT_TEMPLATE = (
    "User has been in the server for: {membership_duration}\n"
    "Previous messages in server: {message_count}\n\n"
    "Evaluate this Discord message:\n\n{content}"
)


async def _fetch_image_attachments(message: Message) -> list[ImageInput]:
    images: list[ImageInput] = []
    for attachment in message.attachments:
        if len(images) >= MAX_IMAGES:
            break
        media_type = attachment.content_type or ""
        if not media_type.startswith("image/"):
            continue
        if attachment.size > MAX_IMAGE_BYTES:
            log.debug(f"Skipping oversized image attachment ({attachment.size} bytes)")
            continue
        try:
            data = await attachment.read()
        except Exception as e:
            log.warning(f"Failed to fetch image attachment {attachment.url}: {e}")
            continue
        images.append(
            ImageInput(data=data, media_type=media_type.split(";")[0].strip())
        )
    return images


class LLMScamChecker:
    def __init__(self) -> None:
        self._provider: LLMProvider | None = create_provider(cfg.scam_detection.llm)
        self.enabled = self._provider is not None

    async def check(self, message: Message, *, user_msg_count: int) -> str | None:
        """Evaluate a message for social engineering using an LLM.

        Returns a reason string if the message is likely a scam, None otherwise.
        """
        if not self._provider:
            return None

        data = message_to_dict(message)

        images = await _fetch_image_attachments(message)
        if images:
            data["image_attachments_sent_to_model"] = len(images)

        content = json.dumps(data, indent=2)

        membership_duration = "unknown"
        if isinstance(message.author, Member) and message.author.joined_at:
            membership_duration = humanize.naturaltime(
                utcnow() - message.author.joined_at
            )

        prev_user_msg_count = user_msg_count - 1

        user_message = USER_PROMPT_TEMPLATE.format(
            content=content,
            membership_duration=membership_duration,
            message_count=prev_user_msg_count,
        )
        result = await self._provider.complete_structured(
            SYSTEM_PROMPT,
            user_message,
            ScamCheckResult,
            max_tokens=MAX_OUTPUT_TOKENS,
            images=images or None,
        )
        log.debug(
            f"AI scam check ({cfg.scam_detection.llm.provider}/{cfg.scam_detection.llm.model}): {result}"
        )

        if result.is_scam:
            return result.reason or "Unknown"

        return None
