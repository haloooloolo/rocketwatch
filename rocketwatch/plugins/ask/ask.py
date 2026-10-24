import asyncio
import contextlib
import logging
import re
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import discord
from discord import File, Message, ui
from discord.ext import commands
from pydantic import BaseModel, Field

from rocketwatch.bot import RocketWatch
from rocketwatch.plugins.ask.prompt import build_system_prompt
from rocketwatch.plugins.ask.tools import WEB_TOOLS, AskTools
from rocketwatch.utils.agent import Turn, Usage, estimate_cost, run_agent
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import CustomColors, Embed
from rocketwatch.utils.llm import AnthropicProvider, ImageInput, create_provider

log = logging.getLogger("rocketwatch.ask")

MAX_CHAIN = 10
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TEXT_BYTES = 100_000
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TEXT_TYPES = ("text/", "application/json")
MAX_MESSAGE_TEXT = 4000
MAX_BLOCKS = 20
MAX_GALLERY = 10
FULL_ANSWER_FILE = "answer.md"
# past the time limit the agent wraps up; the timeout is a backstop for that
ANSWER_TIME_LIMIT = 10 * 60
ANSWER_TIMEOUT = 12 * 60
SHRUG = "\N{SHRUG}"
# the usage line under every answer: it marks a message as one, and is kept out
# of the history so the model doesn't imitate it
USAGE_LINE = re.compile(r"^-# \d+ tool calls? · .*$\n?", re.M)
MESSAGE_LINK = re.compile(
    r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(?:@me|\d+)/(\d+)/(\d+)"
)
MAX_LINKS = 3


class TextBlock(BaseModel):
    text: str = Field(description="Discord markdown")


class ImagesBlock(BaseModel):
    images: list[str] = Field(
        description="Chart filenames from make_chart, shown as a gallery"
    )


class Answer(BaseModel):
    blocks: list[TextBlock | ImagesBlock]
    attachments: list[str] = Field(
        description="Filenames from save_file or make_chart offered as downloads"
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def usage_footer(usage: Usage, model: str) -> str:
    def thousands(n: int) -> str:
        return f"{n / 1000:.0f}k" if n >= 1000 else str(n)

    parts = [
        f"{usage.tool_calls} tool call{'' if usage.tool_calls == 1 else 's'}",
        f"{thousands(usage.input_tokens + usage.output_tokens)} tokens",
    ]
    if usage.input_tokens:
        parts.append(f"{usage.cached_tokens / usage.input_tokens:.0%} cached")
    if (cost := estimate_cost(usage, model)) is not None:
        parts.append(f"≈${cost:.2f}" if cost >= 0.01 else "<$0.01")
    return " · ".join(parts)


def render_answer(
    answer: Answer, files: dict[str, bytes], footer_text: str
) -> tuple[ui.LayoutView, list[File]]:
    footer = f"-# {footer_text}"
    budget = MAX_MESSAGE_TEXT - len(footer)
    items: list[ui.Item[ui.LayoutView]] = []
    shown: list[str] = []
    cut = len(answer.blocks) > MAX_BLOCKS
    for block in answer.blocks[:MAX_BLOCKS]:
        if isinstance(block, TextBlock):
            if not block.text.strip():
                continue
            if budget < 2:
                cut = True
                continue
            text = _clip(block.text, budget)
            cut = cut or text != block.text
            budget -= len(text)
            items.append(ui.TextDisplay(text))
            continue
        names = [
            n
            for n in dict.fromkeys(block.images)
            if n.endswith(".png") and n in files and n not in shown
        ][:MAX_GALLERY]
        if names:
            shown += names
            gallery = (discord.MediaGalleryItem(f"attachment://{n}") for n in names)
            items.append(ui.MediaGallery(*gallery))

    attached = dict(files)
    downloads = list(answer.attachments)
    if cut:
        texts = [b.text for b in answer.blocks if isinstance(b, TextBlock)]
        attached[FULL_ANSWER_FILE] = "\n\n".join(texts).encode()
        downloads.append(FULL_ANSWER_FILE)
    for name in dict.fromkeys(downloads):
        if name in attached and name not in shown:
            shown.append(name)
            items.append(ui.File(f"attachment://{name}"))
    items.append(ui.TextDisplay(footer))

    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=CustomColors.ORANGE))
    return view, [File(BytesIO(attached[n]), n) for n in shown]


def _component_texts(components: list[Any]) -> list[str]:
    texts: list[str] = []
    for component in components:
        if isinstance(component, discord.components.TextDisplay):
            texts.append(component.content)
        else:
            texts += _component_texts(getattr(component, "children", []))
    return texts


def _is_text(att: discord.Attachment) -> bool:
    content_type = (att.content_type or "").split(";")[0]
    return content_type.startswith(TEXT_TYPES) and att.size <= MAX_TEXT_BYTES


def message_text(message: Message) -> str:
    parts: list[str] = [message.content] if message.content else []
    for e in message.embeds:
        if e.title:
            parts.append(f"**{e.title}**")
        if e.description:
            parts.append(e.description)
        parts += [f"{f.name}: {f.value}" for f in e.fields]
    parts += _component_texts(message.components)
    return "\n".join(parts)


class Ask(commands.Cog):
    def __init__(self, bot: RocketWatch, llm: AnthropicProvider) -> None:
        self.bot = bot
        self.llm = llm
        self.system_prompt = build_system_prompt()

    def _is_answer(self, message: Message) -> bool:
        # recognized by the usage line under it, so this survives restarts
        assert self.bot.user is not None
        return message.author.id == self.bot.user.id and any(
            USAGE_LINE.match(text) for text in _component_texts(message.components)
        )

    def _pings_bot(self, message: Message) -> bool:
        # mentions include replies that ping the bot, so turning the ping off opts out
        assert self.bot.user is not None
        return any(user.id == self.bot.user.id for user in message.mentions)

    def _is_question(self, message: Message) -> bool:
        if message.author.id != cfg.discord.owner.user_id:
            return False
        return message.guild is None or self._pings_bot(message)

    def _is_stranger_question(self, message: Message) -> bool:
        return (
            not message.author.bot
            and message.author.id != cfg.discord.owner.user_id
            and self._pings_bot(message)
        )

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        if not self._is_question(message):
            if self._is_stranger_question(message):
                with contextlib.suppress(discord.HTTPException):
                    await message.add_reaction(SHRUG)
            return

        usage = Usage()
        async with message.channel.typing():
            try:
                tools = AskTools(self.bot.db, cfg.secrets.github)
                async with asyncio.timeout(ANSWER_TIMEOUT):
                    result = await run_agent(
                        self.llm,
                        self.system_prompt,
                        await self._build_turns(message),
                        tools.definitions(),
                        Answer,
                        server_tools=WEB_TOOLS,
                        code_execution=True,
                        time_limit=ANSWER_TIME_LIMIT,
                        usage=usage,
                    )
                view, files = render_answer(
                    result.output,
                    tools.files,
                    usage_footer(result.usage, self.llm.model),
                )
                await message.reply(view=view, files=files, mention_author=False)
            except TimeoutError:
                log.warning("Gave up on a question after %ds", ANSWER_TIMEOUT)
                timeout = Embed(
                    title="That took too long",
                    description=f"I stopped after {ANSWER_TIMEOUT // 60} minutes. "
                    "A narrower question may work.",
                    color=CustomColors.RED,
                )
                timeout.set_footer(text=usage_footer(usage, self.llm.model))
                await message.reply(embed=timeout, mention_author=False)
                return
            except Exception as err:
                log.exception("Failed to answer question")
                await self.bot.report_error(err)
                error = Embed(
                    title="Couldn't answer that",
                    description=f"`{err!r}`"[:4096],
                    color=CustomColors.RED,
                )
                error.set_footer(text=usage_footer(usage, self.llm.model))
                await message.reply(embed=error, mention_author=False)
                return

    @staticmethod
    async def _parent(message: Message) -> Message | None:
        ref = message.reference
        if ref is None or ref.message_id is None:
            return None
        if isinstance(ref.resolved, Message):
            return ref.resolved
        if ref.channel_id != message.channel.id:
            return None
        try:
            return await message.channel.fetch_message(ref.message_id)
        except discord.HTTPException:
            return None

    async def _build_turns(self, message: Message) -> list[Turn]:
        chain = [message]
        while len(chain) < MAX_CHAIN and (parent := await self._parent(chain[-1])):
            chain.append(parent)

        turns: list[Turn] = []
        images_left = MAX_IMAGES
        for m in reversed(chain[1:]):
            # the conversation has to open with a user turn
            if turns and self._is_answer(m):
                turns.append(Turn("assistant", await self._read_answer(m)))
                continue
            text, images = await self._read_message(m, images_left)
            images_left -= len(images)
            turns.append(Turn("user", text, images))

        for linked in await self._linked_messages(message):
            if isinstance(linked, str):
                turns.append(Turn("user", linked))
                continue
            text, images = await self._read_message(linked, images_left, quote=True)
            images_left -= len(images)
            turns.append(Turn("user", text, images))

        text, images = await self._read_message(message, images_left)
        now = datetime.now(UTC)
        text = f"Current time: {now:%Y-%m-%d %H:%M} UTC (unix {int(now.timestamp())})\n\n{text}"
        turns.append(Turn("user", text, images))
        return turns

    async def _linked_messages(self, message: Message) -> list[Message | str]:
        """Messages linked in the question, or a note for each that can't be read."""
        links = list(dict.fromkeys(MESSAGE_LINK.findall(message.content)))
        linked: list[Message | str] = []
        for channel_id, message_id in links[:MAX_LINKS]:
            try:
                channel = self.bot.get_channel(
                    int(channel_id)
                ) or await self.bot.fetch_channel(int(channel_id))
                if isinstance(channel, discord.abc.Messageable):
                    linked.append(await channel.fetch_message(int(message_id)))
                    continue
            except discord.HTTPException:
                pass
            linked.append(f"[Linked message {message_id} couldn't be opened]")
        return linked

    async def _read_message(
        self, message: Message, images_left: int, *, quote: bool = False
    ) -> tuple[str, list[ImageInput]]:
        assert self.bot.user is not None
        if message.author.id == cfg.discord.owner.user_id and not quote:
            text = re.sub(rf"<@!?{self.bot.user.id}>", "", message.content).strip()
        else:
            ts = int(message.created_at.timestamp())
            quoted = USAGE_LINE.sub("", message_text(message)).strip()
            text = (
                f"[Quoted message from {message.author.display_name} at <t:{ts}>]\n"
                f"{quoted}"
            )

        images: list[ImageInput] = []
        for att in message.attachments:
            content_type = (att.content_type or "").split(";")[0]
            if content_type in IMAGE_TYPES:
                if len(images) < images_left and att.size <= MAX_IMAGE_BYTES:
                    images.append(ImageInput(await att.read(), content_type))
            elif _is_text(att):
                content = (await att.read()).decode(errors="replace")
                text += f"\n\n[Attachment {att.filename}]\n{content}"
        return text, images

    @staticmethod
    async def _read_answer(message: Message) -> str:
        full: str | None = None
        extras: list[str] = []
        for att in message.attachments:
            if not _is_text(att):
                continue
            content = (await att.read()).decode(errors="replace")
            if att.filename == FULL_ANSWER_FILE:
                full = content
            else:
                extras.append(f"[Attachment {att.filename}]\n{content}")
        text = full or USAGE_LINE.sub("", message_text(message)).strip()
        return "\n\n".join([text, *extras])


async def setup(bot: RocketWatch) -> None:
    llm = create_provider(cfg.ask.llm)
    if not isinstance(llm, AnthropicProvider):
        log.warning("Not loading: ask.llm must be an Anthropic provider")
        return
    await bot.add_cog(Ask(bot, llm))
