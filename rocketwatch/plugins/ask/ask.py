import asyncio
import contextlib
import logging
import re
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import discord
from cachetools import LRUCache
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
# the usage line under an answer, kept out of the history so it isn't imitated
USAGE_LINE = re.compile(r"^-# \d+ tool calls? · .*$\n?", re.M)


class TextBlock(BaseModel):
    text: str = Field(description="Discord markdown")


class ImagesBlock(BaseModel):
    images: list[str] = Field(
        description="Chart filenames from make_chart, shown as a gallery"
    )


class Answer(BaseModel):
    card: bool = Field(description="Wrap the reply in a colored card")
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
    if answer.card:
        view.add_item(ui.Container(*items, accent_colour=CustomColors.ORANGE))
    else:
        for item in items:
            view.add_item(item)
    return view, [File(BytesIO(attached[n]), n) for n in shown]


def _component_texts(components: list[Any]) -> list[str]:
    texts: list[str] = []
    for component in components:
        if isinstance(component, discord.components.TextDisplay):
            texts.append(component.content)
        else:
            texts += _component_texts(getattr(component, "children", []))
    return texts


def message_text(message: Message) -> str:
    parts: list[str] = [message.content] if message.content else []
    for e in message.embeds:
        if e.title:
            parts.append(f"**{e.title}**")
        if e.description:
            parts.append(e.description)
        parts += [f"{f.name}: {f.value}" for f in e.fields]
    parts += _component_texts(list(message.components))
    return "\n".join(parts)


class Ask(commands.Cog):
    def __init__(self, bot: RocketWatch, llm: AnthropicProvider) -> None:
        self.bot = bot
        self.llm = llm
        self.system_prompt = build_system_prompt()
        # ids of our answers, so replying to one continues the conversation
        self._answers: LRUCache[int, bool] = LRUCache(maxsize=1024)

    def _is_question(self, message: Message) -> bool:
        if message.author.id != cfg.discord.owner.user_id:
            return False
        assert self.bot.user is not None
        if message.guild is None or self.bot.user.id in message.raw_mentions:
            return True
        ref = message.reference
        return ref is not None and ref.message_id in self._answers

    def _is_stranger_mention(self, message: Message) -> bool:
        assert self.bot.user is not None
        return (
            not message.author.bot
            and message.author.id != cfg.discord.owner.user_id
            and self.bot.user.id in message.raw_mentions
        )

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        if not self._is_question(message):
            if self._is_stranger_mention(message):
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
                reply = await message.reply(
                    view=view, files=files, mention_author=False
                )
            except TimeoutError:
                log.warning("Gave up on a question after %ds", ANSWER_TIMEOUT)
                timeout = Embed(
                    title="That took too long",
                    description=f"I stopped after {ANSWER_TIMEOUT // 60} minutes. "
                    "A narrower question may work.",
                    color=CustomColors.RED,
                )
                timeout.set_footer_parts([usage_footer(usage, self.llm.model)])
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
                error.set_footer_parts([usage_footer(usage, self.llm.model)])
                await message.reply(embed=error, mention_author=False)
                return

        self._answers[reply.id] = True

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
        for m in reversed(chain):
            # the conversation has to open with a user turn
            if m.id in self._answers and turns:
                text = USAGE_LINE.sub("", message_text(m)).strip()
                turns.append(Turn("assistant", text))
                continue
            text, images = await self._read_message(m, images_left)
            images_left -= len(images)
            if m is message:
                now = datetime.now(UTC)
                text = f"Current time: {now:%Y-%m-%d %H:%M} UTC (unix {int(now.timestamp())})\n\n{text}"
            turns.append(Turn("user", text, images))
        return turns

    async def _read_message(
        self, message: Message, images_left: int
    ) -> tuple[str, list[ImageInput]]:
        assert self.bot.user is not None
        if message.author.id == cfg.discord.owner.user_id:
            text = re.sub(rf"<@!?{self.bot.user.id}>", "", message.content).strip()
        else:
            ts = int(message.created_at.timestamp())
            text = (
                f"[Quoted message from {message.author.display_name} at <t:{ts}>]\n"
                f"{message_text(message)}"
            )

        images: list[ImageInput] = []
        for att in message.attachments:
            content_type = (att.content_type or "").split(";")[0]
            if content_type in IMAGE_TYPES:
                if len(images) < images_left and att.size <= MAX_IMAGE_BYTES:
                    images.append(ImageInput(await att.read(), content_type))
            elif content_type.startswith(TEXT_TYPES) and att.size <= MAX_TEXT_BYTES:
                content = (await att.read()).decode(errors="replace")
                text += f"\n\n[Attachment {att.filename}]\n{content}"
        return text, images


async def setup(bot: RocketWatch) -> None:
    llm = create_provider(cfg.ask.llm)
    if not isinstance(llm, AnthropicProvider):
        log.warning("Not loading: ask.llm must be an Anthropic provider")
        return
    await bot.add_cog(Ask(bot, llm))
