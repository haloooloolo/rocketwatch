import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import discord
from discord import Message, MessageSnapshot

from rocketwatch.utils.llm import DocumentInput, ImageInput

# the usage line under every answer: it marks a message as one, and is kept out
# of the history so the model doesn't imitate it
USAGE_LINE = re.compile(r"^-# \d+ tool calls? · .*$\n?", re.M)
MESSAGE_LINK = re.compile(
    r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(?:@me|\d+)/(\d+)/(\d+)"
)
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TEXT_TYPES = ("text/", "application/json")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TEXT_BYTES = 100_000
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=10)


@dataclass
class MediaBudget:
    """How many more images and PDFs may go to the model."""

    images: int
    documents: int


@dataclass
class MessageContent:
    text: str
    images: list[ImageInput] = field(default_factory=list)
    documents: list[DocumentInput] = field(default_factory=list)


def component_texts(components: list[Any]) -> list[str]:
    texts: list[str] = []
    for component in components:
        if isinstance(component, discord.components.TextDisplay):
            texts.append(component.content)
        else:
            texts += component_texts(getattr(component, "children", []))
    return texts


def _content_type(att: discord.Attachment) -> str:
    return (att.content_type or "").split(";")[0]


def is_text(att: discord.Attachment) -> bool:
    return _content_type(att).startswith(TEXT_TYPES) and att.size <= MAX_TEXT_BYTES


def take_image(
    att: discord.Attachment, data: bytes, budget: MediaBudget
) -> ImageInput | None:
    """An attachment's data as an image for the model, while the budget lasts."""
    kind = _content_type(att)
    if kind not in IMAGE_TYPES or len(data) > MAX_IMAGE_BYTES or budget.images < 1:
        return None
    budget.images -= 1
    return ImageInput(data, kind)


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{n / 1024:.0f} KB"


def _render_embed(embed: discord.Embed, image_urls: list[str]) -> list[str]:
    lines: list[str] = []
    if embed.author.name:
        lines.append(
            embed.author.name + (f" <{embed.author.url}>" if embed.author.url else "")
        )
    if embed.title:
        lines.append(f"**{embed.title}**" + (f" <{embed.url}>" if embed.url else ""))
    elif embed.url:
        lines.append(embed.url)
    if embed.description:
        lines.append(embed.description)
    lines += [f"{f.name}: {f.value}" for f in embed.fields]
    if embed.footer.text:
        lines.append(f"Footer: {embed.footer.text}")
    if embed.timestamp:
        lines.append(f"Time: <t:{int(embed.timestamp.timestamp())}>")
    for media in (embed.image, embed.thumbnail):
        if media.url:
            image_urls.append(media.proxy_url or media.url)
    if embed.video.url:
        lines.append(f"Video: {embed.video.url}")
    return lines


def _render_components(
    components: list[Any], lines: list[str], image_urls: list[str]
) -> None:
    c = discord.components
    for component in components:
        if isinstance(component, c.TextDisplay):
            lines.append(component.content)
        elif isinstance(component, c.Button) and (component.label or component.url):
            target = f" → {component.url}" if component.url else ""
            lines.append(f"[Button {component.label or ''}{target}]")
        elif isinstance(component, c.SelectMenu):
            options = ", ".join(o.label for o in component.options)
            lines.append(f"[Menu: {options or component.placeholder or ''}]")
        elif isinstance(component, c.MediaGalleryComponent):
            image_urls += [i.media.proxy_url or i.media.url for i in component.items]
        elif isinstance(component, c.ThumbnailComponent):
            image_urls.append(component.media.proxy_url or component.media.url)
        children = list(getattr(component, "children", []))
        if (accessory := getattr(component, "accessory", None)) is not None:
            children.append(accessory)
        _render_components(children, lines, image_urls)


def message_text(message: Message) -> str:
    """The text of a message, without reading its attachments."""
    lines: list[str] = [message.content] if message.content else []
    unread: list[str] = []
    for embed in message.embeds:
        lines += _render_embed(embed, unread)
    _render_components(list(message.components), lines, unread)
    return "\n".join(lines)


async def _read_attachment(
    att: discord.Attachment, budget: MediaBudget, out: MessageContent
) -> str:
    kind = _content_type(att)
    name = att.filename + (" (spoiler)" if att.is_spoiler() else "")
    alt = f": {att.description}" if att.description else ""
    if kind in IMAGE_TYPES:
        if budget.images > 0 and att.size <= MAX_IMAGE_BYTES:
            out.images.append(ImageInput(await att.read(), kind))
            budget.images -= 1
            return f"[Image {name}{alt}]"
        return f"[Image {name}{alt}, not shown]"
    if kind == "application/pdf":
        if budget.documents > 0 and att.size <= MAX_DOCUMENT_BYTES:
            out.documents.append(DocumentInput(await att.read()))
            budget.documents -= 1
            return f"[PDF {name}]"
        return f"[PDF {name}, {_size(att.size)}, not shown]"
    if is_text(att):
        content = (await att.read()).decode(errors="replace")
        return f"[Attachment {name}]\n{content}"
    if att.is_voice_message():
        duration = f", {att.duration:.0f} s" if att.duration else ""
        return f"[Voice message{duration}]"
    return f"[File {name} ({kind or 'unknown type'}, {_size(att.size)})]"


async def _download_image(url: str) -> ImageInput | None:
    try:
        async with (
            aiohttp.ClientSession(timeout=IMAGE_TIMEOUT) as session,
            session.get(url) as resp,
        ):
            kind = resp.headers.get("Content-Type", "").split(";")[0]
            if resp.status != 200 or kind not in IMAGE_TYPES:
                return None
            data = await resp.content.read(MAX_IMAGE_BYTES + 1)
    except (aiohttp.ClientError, TimeoutError):
        return None
    return ImageInput(data, kind) if len(data) <= MAX_IMAGE_BYTES else None


async def read_message(
    message: Message | MessageSnapshot,
    budget: MediaBudget,
    *,
    text: str | None = None,
) -> MessageContent:
    """Everything a message holds that the model can use: text, embeds,
    components, polls, forwarded messages and reactions as text, and images and
    PDFs as media while the budget lasts. text replaces the message's own."""
    out = MessageContent("")
    if text is None:
        text = (
            message.clean_content if isinstance(message, Message) else message.content
        )
    lines: list[str] = [text] if text else []
    image_urls: list[str] = []
    for embed in message.embeds:
        lines += _render_embed(embed, image_urls)
    _render_components(list(message.components), lines, image_urls)
    lines += [f"[Sticker {sticker.name}]" for sticker in message.stickers]
    if isinstance(message, Message) and message.poll is not None:
        poll = message.poll
        answers = ", ".join(f"{a.text} ({a.vote_count})" for a in poll.answers)
        state = "final" if poll.is_finalised() else "open"
        lines.append(f"[Poll, {state}: {poll.question}: {answers}]")

    for att in message.attachments:
        lines.append(await _read_attachment(att, budget, out))
    # embeds show their own attachments again; those are read already
    attached = {
        urlsplit(u).path for a in message.attachments for u in (a.url, a.proxy_url)
    }
    for url in dict.fromkeys(image_urls):
        if url.startswith("attachment://") or urlsplit(url).path in attached:
            continue
        if budget.images > 0 and (image := await _download_image(url)):
            out.images.append(image)
            budget.images -= 1
            lines.append(f"[Image {url}]")
        else:
            lines.append(f"[Image {url}, not shown]")

    if isinstance(message, Message):
        for snapshot in message.message_snapshots:
            forwarded = await read_message(snapshot, budget)
            lines.append(f"[Forwarded message]\n{forwarded.text}")
            out.images += forwarded.images
            out.documents += forwarded.documents
        if message.reactions:
            reactions = ", ".join(
                f"{getattr(r.emoji, 'name', r.emoji)} {r.count}"
                for r in message.reactions
            )
            lines.append(f"Reactions: {reactions}")
    out.text = "\n".join(lines)
    return out
