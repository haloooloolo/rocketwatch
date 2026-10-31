import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import discord
from discord import File, Message, ui
from discord.ext import commands
from pydantic import BaseModel, Field

from rocketwatch.bot import RocketWatch
from rocketwatch.plugins.ask.budget import DailyBudget
from rocketwatch.plugins.ask.messages import (
    MESSAGE_LINK,
    USAGE_LINE,
    MediaBudget,
    MessageContent,
    component_texts,
    is_text,
    message_text,
    read_message,
    readable_channel,
    take_image,
)
from rocketwatch.plugins.ask.prompt import build_system_prompt
from rocketwatch.plugins.ask.tools import PUBLIC_COLLECTIONS, WEB_TOOLS, AskTools
from rocketwatch.utils.agent import PRICES, Turn, Usage, run_agent
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import CustomColors, Embed
from rocketwatch.utils.llm import AnthropicProvider, create_provider

log = logging.getLogger("rocketwatch.ask")

MAX_CHAIN = 10
MAX_IMAGES = 4
MAX_DOCUMENTS = 1
MAX_MESSAGE_TEXT = 4000
MAX_BLOCKS = 20
MAX_GALLERY = 10
# Discord's limit per message
MAX_ATTACHMENTS = 10
GALLERY_TYPES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
FULL_ANSWER_FILE = "answer.md"
CHART_STYLE = Path(__file__).parents[2] / "resources" / "rocketwatch.mplstyle"
# past the time limit the agent wraps up; the timeout is a backstop for that
ANSWER_TIME_LIMIT = 10 * 60
ANSWER_TIMEOUT = 12 * 60
SHRUG = "\N{SHRUG}"
BUSY = "\N{HOURGLASS WITH FLOWING SAND}"
MAX_LINKS = 3
MAX_RUN_COST = 1.0
MIN_RUN_COST = 0.25
CONFIRM_TIMEOUT = 10 * 60


class TextBlock(BaseModel):
    text: str = Field(description="Discord markdown")


class ImagesBlock(BaseModel):
    images: list[str] = Field(
        description="Image filenames from OUTPUT_DIR, shown as a gallery"
    )


class Answer(BaseModel):
    blocks: list[TextBlock | ImagesBlock]
    attachments: list[str] = Field(
        description="Filenames from save_file or OUTPUT_DIR offered as downloads"
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def usage_footer(usage: Usage) -> str:
    def thousands(n: int) -> str:
        return f"{n / 1000:.0f}k" if n >= 1000 else str(n)

    parts = [
        f"{usage.tool_calls} tool call{'' if usage.tool_calls == 1 else 's'}",
        f"{thousands(usage.input_tokens + usage.output_tokens)} tokens",
    ]
    if usage.input_tokens:
        parts.append(f"{usage.cached_tokens / usage.input_tokens:.0%} cached")
    parts.append(f"≈${usage.cost:.2f}" if usage.cost >= 0.01 else "<$0.01")
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
            if n.lower().endswith(GALLERY_TYPES) and n in files and n not in shown
        ][: min(MAX_GALLERY, MAX_ATTACHMENTS - 1 - len(shown))]
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
        # the last slot is kept for the full answer
        full = len(shown) >= MAX_ATTACHMENTS - 1 and name != FULL_ANSWER_FILE
        if name in attached and name not in shown and not full:
            shown.append(name)
            items.append(ui.File(f"attachment://{name}"))
    items.append(ui.TextDisplay(footer))

    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=CustomColors.ORANGE))
    return view, [File(BytesIO(attached[n]), n) for n in shown]


class ConfirmView(ui.View):
    """Asks whether a reply was meant as a question, since replying to the bot
    pings it by default."""

    def __init__(self, asker_id: int, on_yes: Callable[[], Awaitable[None]]) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT)
        self.asker_id = asker_id
        self.on_yes = on_yes
        self.message: Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.asker_id:
            return True
        await interaction.response.send_message(
            "Only the person who replied can choose.", ephemeral=True
        )
        return False

    async def _close(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.defer()
        if interaction.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await interaction.message.delete()

    @ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes(
        self, interaction: discord.Interaction, _: ui.Button["ConfirmView"]
    ) -> None:
        await self._close(interaction)
        await self.on_yes()

    @ui.button(label="Shut Up", style=discord.ButtonStyle.danger)
    async def shut_up(
        self, interaction: discord.Interaction, _: ui.Button["ConfirmView"]
    ) -> None:
        await self._close(interaction)

    async def on_timeout(self) -> None:
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.delete()


class Ask(commands.Cog):
    def __init__(self, bot: RocketWatch, llm: AnthropicProvider) -> None:
        self.bot = bot
        self.llm = llm
        self.system_prompt = build_system_prompt()
        self.container_files = [(CHART_STYLE.name, CHART_STYLE.read_bytes())]
        self.budget = DailyBudget(
            bot.db["ask_budget"],
            cfg.ask.daily_budget_cents / 100,
            per_run=MAX_RUN_COST,
            min_run=MIN_RUN_COST,
        )
        self.running: set[int] = set()

    def _is_answer(self, message: Message) -> bool:
        # recognized by the usage line under it, so this survives restarts
        assert self.bot.user is not None
        return message.author.id == self.bot.user.id and any(
            USAGE_LINE.match(text) for text in component_texts(message.components)
        )

    def _pings_bot(self, message: Message) -> bool:
        # mentions include replies that ping the bot, so turning the ping off opts out
        assert self.bot.user is not None
        return any(user.id == self.bot.user.id for user in message.mentions)

    def _names_bot(self, message: Message) -> bool:
        # raw mentions come from the text only, not from the reply ping
        assert self.bot.user is not None
        return self.bot.user.id in message.raw_mentions

    @staticmethod
    def _in_server(message: Message) -> bool:
        return (
            message.guild is not None
            and message.guild.id == cfg.rocketpool.support.server_id
        )

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        if message.author.bot or not self._pings_bot(message):
            return
        if not self._in_server(message):
            with contextlib.suppress(discord.HTTPException):
                await message.add_reaction(SHRUG)
            return
        if self._names_bot(message):
            await self._answer(message)
            return
        view = ConfirmView(message.author.id, lambda: self._answer(message))
        view.message = await message.reply(
            "Do you want me to respond?", view=view, mention_author=False
        )

    async def _answer(self, message: Message) -> None:
        if message.author.id in self.running:
            with contextlib.suppress(discord.HTTPException):
                await message.add_reaction(BUSY)
            return
        key = object()
        max_cost = await self.budget.hold(key)
        if max_cost is None:
            spent = Embed(
                title="Out of budget for today",
                description="Today's budget for questions is used up, or held by "
                "questions still running. It resets "
                f"<t:{int(self.budget.resets_at().timestamp())}:R>.",
                color=CustomColors.RED,
            )
            await message.reply(embed=spent, mention_author=False)
            return

        self.running.add(message.author.id)
        usage = Usage()
        try:
            await self._run(message, usage, max_cost)
        finally:
            self.running.discard(message.author.id)
            await self.budget.settle(key, usage.cost)

    async def _run(self, message: Message, usage: Usage, max_cost: float) -> None:
        owner = message.author.id == cfg.discord.owner.user_id
        async with message.channel.typing():
            try:
                tools = AskTools(
                    self.bot.db,
                    cfg.secrets.github,
                    self.bot,
                    reader=message.author,
                    collections=None if owner else PUBLIC_COLLECTIONS,
                )
                async with asyncio.timeout(ANSWER_TIMEOUT):
                    turns, answer_files = await self._build_turns(message)
                    result = await run_agent(
                        self.llm,
                        self.system_prompt,
                        turns,
                        tools.definitions(),
                        Answer,
                        server_tools=WEB_TOOLS,
                        code_execution=True,
                        time_limit=ANSWER_TIME_LIMIT,
                        max_cost=max_cost,
                        usage=usage,
                        container_files=[*self.container_files, *answer_files.items()],
                    )
                view, files = render_answer(
                    result.output,
                    {**result.files, **tools.files},
                    usage_footer(result.usage),
                )
                # the model writes what others ask for, so it mustn't ping anyone
                await message.reply(
                    view=view,
                    files=files,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except TimeoutError:
                log.warning("Gave up on a question after %ds", ANSWER_TIMEOUT)
                timeout = Embed(
                    title="That took too long",
                    description=f"I stopped after {ANSWER_TIMEOUT // 60} minutes. "
                    "A narrower question may work.",
                    color=CustomColors.RED,
                )
                timeout.set_footer(text=usage_footer(usage))
                await message.reply(embed=timeout, mention_author=False)
                return
            except Exception as err:
                log.exception("Failed to answer question")
                await self.bot.report_error(err)
                # errors can hold endpoint URLs with keys, so they go to the owner only
                error = Embed(
                    title="Couldn't answer that",
                    description="Something went wrong. It has been reported.",
                    color=CustomColors.RED,
                )
                error.set_footer(text=usage_footer(usage))
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

    async def _build_turns(
        self, message: Message
    ) -> tuple[list[Turn], dict[str, bytes]]:
        """The conversation for the model, and the files attached to earlier
        answers in it, for the sandbox."""
        chain = [message]
        while len(chain) < MAX_CHAIN and (parent := await self._parent(chain[-1])):
            chain.append(parent)

        def user_turn(content: MessageContent) -> Turn:
            return Turn("user", content.text, content.images, content.documents)

        # built newest first, so the question's own media gets the budget first
        budget = MediaBudget(MAX_IMAGES, MAX_DOCUMENTS)
        asker = message.author.id
        question = await self._read_message(message, budget, asker=asker)
        now = datetime.now(UTC)
        question.text = f"Current time: {now:%Y-%m-%d %H:%M} UTC (unix {int(now.timestamp())})\n\n{question.text}"
        turns = [user_turn(question)]

        for link, linked in reversed(await self._linked_messages(message)):
            if linked is None:
                turns.append(
                    Turn("user", f"[Linked message {link} couldn't be opened]")
                )
                continue
            linked_content = await self._read_message(
                linked, budget, asker=asker, link=link
            )
            turns.append(user_turn(linked_content))

        files: dict[str, bytes] = {}
        for m in chain[1:]:
            # the conversation has to open with a user turn
            if m is not chain[-1] and self._is_answer(m):
                text, images, attached = await self._read_answer(m, budget)
                # a newer answer's file wins a shared name
                files = attached | files
                if images.images:
                    turns.append(user_turn(images))
                turns.append(Turn("assistant", text))
                continue
            turns.append(user_turn(await self._read_message(m, budget, asker=asker)))
        turns.reverse()
        return turns, files

    async def _linked_messages(
        self, message: Message
    ) -> list[tuple[str, Message | None]]:
        """Each message linked in the question, or None where the asker can't
        read it."""
        links = {m[0]: m.groups() for m in MESSAGE_LINK.finditer(message.content)}
        linked: list[tuple[str, Message | None]] = []
        for link, (channel_id, message_id) in list(links.items())[:MAX_LINKS]:
            channel = await readable_channel(self.bot, message.author, int(channel_id))
            found: Message | None = None
            if channel is not None:
                with contextlib.suppress(discord.HTTPException):
                    found = await channel.fetch_message(int(message_id))
            linked.append((link, found))
        return linked

    async def _read_message(
        self,
        message: Message,
        budget: MediaBudget,
        *,
        asker: int,
        link: str | None = None,
    ) -> MessageContent:
        assert self.bot.user is not None
        if message.author.id == asker and link is None:
            text = re.sub(rf"<@!?{self.bot.user.id}>", "", message.content).strip()
            return await read_message(message, budget, text=text)
        content = await read_message(message, budget)
        ts = int(message.created_at.timestamp())
        source = f"Linked message {link}" if link else "Quoted message"
        quoted = USAGE_LINE.sub("", content.text).strip()
        name = message.author.display_name
        content.text = f"[{source} from {name} at <t:{ts}>]\n{quoted}"
        return content

    @staticmethod
    async def _read_answer(
        message: Message, budget: MediaBudget
    ) -> tuple[str, MessageContent, dict[str, bytes]]:
        """An answer's text, its images for a user turn after it (assistant turns
        can't hold images), and its files for the sandbox."""
        full: str | None = None
        extras: list[str] = []
        images = MessageContent("")
        files: dict[str, bytes] = {}
        for att in message.attachments:
            data = await att.read()
            if att.filename == FULL_ANSWER_FILE:
                full = data.decode(errors="replace")
                continue
            files[att.filename] = data
            label = f"[Attachment {att.filename}]"
            if is_text(att):
                label += "\n" + data.decode(errors="replace")
            extras.append(label)
            if image := take_image(att, data, budget):
                images.images.append(image)
                images.text += f"\n[Image {att.filename}]"
        if images.images:
            images.text = "[Images in your previous answer]" + images.text
        text = full or USAGE_LINE.sub("", message_text(message)).strip()
        return "\n\n".join([text, *extras]), images, files


async def setup(bot: RocketWatch) -> None:
    llm = create_provider(cfg.ask.llm)
    if not isinstance(llm, AnthropicProvider):
        log.warning("Not loading: ask.llm must be an Anthropic provider")
        return
    if llm.model not in PRICES:
        log.warning("No prices for %s, so it's charged at the top rate", llm.model)
    await bot.add_cog(Ask(bot, llm))
