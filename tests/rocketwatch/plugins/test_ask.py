import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from rocketwatch.plugins.ask import ask as ask_module
from rocketwatch.plugins.ask.ask import (
    FULL_ANSWER_FILE,
    MAX_MESSAGE_TEXT,
    Answer,
    Ask,
    ImagesBlock,
    TextBlock,
    render_answer,
    setup,
    usage_footer,
)
from rocketwatch.plugins.ask.tools import FileInput
from rocketwatch.utils.agent import AgentResult, AgentTool, Turn, Usage
from rocketwatch.utils.config import LLMConfig, cfg
from rocketwatch.utils.embeds import CustomColors
from rocketwatch.utils.llm import AnthropicProvider
from tests.lib.discord_harness import make_bot

BOT_ID = 999
OWNER_ID = 1  # make_cfg's owner
STRANGER_ID = 7


def _answer(*blocks: TextBlock | ImagesBlock, **overrides: Any) -> Answer:
    values: dict[str, Any] = {
        "blocks": list(blocks) or [TextBlock(text="There are **4,000** nodes.")],
        "attachments": [],
    }
    values.update(overrides)
    return Answer.model_validate(values)


def _components(view: discord.ui.LayoutView) -> list[dict[str, Any]]:
    """Every component in the view, flattened depth-first."""
    flat: list[dict[str, Any]] = []
    stack: list[Any] = list(reversed(view.to_components()))
    while stack:
        node = stack.pop()
        flat.append(node)
        stack.extend(reversed(node.get("components", [])))
    return flat


def _texts(view: discord.ui.LayoutView) -> list[str]:
    text_display = discord.ComponentType.text_display.value
    return [c["content"] for c in _components(view) if c["type"] == text_display]


@pytest.fixture
def cog() -> Ask:
    bot = make_bot()
    bot.user.id = BOT_ID
    return Ask(bot, AnthropicProvider(api_key="k", model="m"))


def _message(
    content: str = "",
    *,
    message_id: int = 100,
    author_id: int = OWNER_ID,
    author_name: str = "owner",
    is_bot: bool = False,
    in_guild: bool = True,
    parent: MagicMock | None = None,
    attachments: list[MagicMock] | None = None,
    embeds: list[discord.Embed] | None = None,
) -> MagicMock:
    message = MagicMock()
    message.id = message_id
    message.content = content
    message.author.id = author_id
    message.author.display_name = author_name
    message.author.bot = is_bot
    message.raw_mentions = [BOT_ID] if f"<@{BOT_ID}>" in content else []
    message.guild = MagicMock() if in_guild else None
    message.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    message.attachments = attachments or []
    message.embeds = embeds or []
    message.components = []
    message.channel.id = 5
    message.reply = AsyncMock(return_value=MagicMock(id=message_id + 1000))
    message.add_reaction = AsyncMock()
    if parent is None:
        message.reference = None
    else:
        message.reference.message_id = parent.id
        message.reference.channel_id = 5
        message.reference.resolved = None
        message.channel.fetch_message = AsyncMock(return_value=parent)
    return message


def _attachment(content_type: str, data: bytes, filename: str = "f") -> MagicMock:
    att = MagicMock()
    att.content_type = content_type
    att.size = len(data)
    att.filename = filename
    att.read = AsyncMock(return_value=data)
    return att


class TestIsQuestion:
    def test_owner_mention(self, cog: Ask) -> None:
        assert cog._is_question(_message(f"<@{BOT_ID}> how many nodes?"))

    def test_owner_without_mention(self, cog: Ask) -> None:
        assert not cog._is_question(_message("how many nodes?"))

    def test_stranger_mention(self, cog: Ask) -> None:
        message = _message(f"<@{BOT_ID}> how many nodes?", author_id=STRANGER_ID)
        assert not cog._is_question(message)

    def test_owner_dm(self, cog: Ask) -> None:
        assert cog._is_question(_message("how many nodes?", in_guild=False))

    def test_reply_to_answer_continues(self, cog: Ask) -> None:
        answer = _message(message_id=50, author_id=BOT_ID)
        cog._answers[answer.id] = True
        assert cog._is_question(_message("and megapools?", parent=answer))

    def test_reply_to_other_bot_message(self, cog: Ask) -> None:
        event = _message(message_id=50, author_id=BOT_ID)
        assert not cog._is_question(_message("what's this?", parent=event))


class TestBuildTurns:
    async def test_question_strips_mention_and_has_time(self, cog: Ask) -> None:
        turns = await cog._build_turns(_message(f"<@{BOT_ID}> how many nodes?"))
        [turn] = turns
        assert turn.role == "user"
        assert turn.text.startswith("Current time: ")
        assert turn.text.endswith("how many nodes?")
        assert f"<@{BOT_ID}>" not in turn.text

    async def test_quoted_message_with_image_and_text_file(self, cog: Ask) -> None:
        quoted = _message(
            "rETH depegged!",
            message_id=10,
            author_id=STRANGER_ID,
            author_name="alice",
            attachments=[
                _attachment("image/png", b"\x89PNG"),
                _attachment("text/csv; charset=utf-8", b"a,b\n1,2", "data.csv"),
                _attachment("application/zip", b"PK"),
            ],
        )
        question = _message(f"<@{BOT_ID}> is this true?", parent=quoted)

        context, asked = await cog._build_turns(question)

        assert context.role == "user"
        assert "alice" in context.text
        assert "rETH depegged!" in context.text
        assert "[Attachment data.csv]\na,b\n1,2" in context.text
        assert [img.media_type for img in context.images] == ["image/png"]
        assert asked.text.endswith("is this true?")

    async def test_follow_up_includes_previous_answer(self, cog: Ask) -> None:
        first = _message(f"<@{BOT_ID}> how many nodes?", message_id=10)
        answer = _message(message_id=11, author_id=BOT_ID, parent=first)
        answer.components = [
            discord.components._component_factory(
                {
                    "type": 17,
                    "components": [
                        {"type": 10, "content": "4,000 nodes"},
                        {
                            "type": 10,
                            "content": "-# 3 tool calls · 20k tokens · ≈$0.12",
                        },
                    ],
                }
            )
        ]
        cog._answers[answer.id] = True
        follow_up = _message("and last year?", message_id=12, parent=answer)

        turns = await cog._build_turns(follow_up)

        assert [t.role for t in turns] == ["user", "assistant", "user"]
        # without the usage line, which the model would otherwise copy
        assert turns[1].text == "4,000 nodes"
        assert turns[2].text.endswith("and last year?")

    async def test_quoted_bot_embed_is_context(self, cog: Ask) -> None:
        event = _message(
            message_id=10,
            author_id=BOT_ID,
            author_name="Rocket Watch",
            embeds=[discord.Embed(title="Megapool deployed", description="node 0x1")],
        )
        [context, _] = await cog._build_turns(
            _message(f"<@{BOT_ID}> who?", parent=event)
        )
        assert context.role == "user"
        assert "**Megapool deployed**\nnode 0x1" in context.text

    async def test_images_are_capped(self, cog: Ask) -> None:
        images = [_attachment("image/jpeg", b"\xff\xd8") for _ in range(6)]
        turns = await cog._build_turns(
            _message(f"<@{BOT_ID}> compare", attachments=images)
        )
        assert len(turns[0].images) == ask_module.MAX_IMAGES

    async def test_deleted_parent_ends_chain(self, cog: Ask) -> None:
        parent = _message(message_id=10)
        question = _message(f"<@{BOT_ID}> hm", parent=parent)
        question.channel.fetch_message.side_effect = discord.NotFound(
            MagicMock(status=404), "gone"
        )
        assert len(await cog._build_turns(question)) == 1


class TestRenderAnswer:
    def test_reply_is_one_card(self) -> None:
        view, files = render_answer(_answer(), {}, footer_text="3 tool calls")
        [card] = view.to_components()
        assert card["type"] == discord.ComponentType.container.value
        assert card["accent_color"] == CustomColors.ORANGE.value
        assert _texts(view) == ["There are **4,000** nodes.", "-# 3 tool calls"]
        assert files == []

    def test_gallery_and_downloads(self) -> None:
        files = {"a.png": b"\x89PNG", "b.png": b"\x89PNG", "nodes.csv": b"x"}
        view, attached = render_answer(
            _answer(
                TextBlock(text="Charts:"),
                ImagesBlock(images=["a.png", "b.png", "missing.png", "nodes.csv"]),
                attachments=["nodes.csv", "a.png", "missing.csv"],
            ),
            files,
            footer_text="1 tool calls",
        )
        components = _components(view)
        [gallery] = [
            c
            for c in components
            if c["type"] == discord.ComponentType.media_gallery.value
        ]
        assert [i["media"]["url"] for i in gallery["items"]] == [
            "attachment://a.png",
            "attachment://b.png",
        ]
        downloads = [
            c["file"]["url"]
            for c in components
            if c["type"] == discord.ComponentType.file.value
        ]
        assert downloads == ["attachment://nodes.csv"]
        assert [f.filename for f in attached] == ["a.png", "b.png", "nodes.csv"]

    def test_blank_text_blocks_are_skipped(self) -> None:
        view, _ = render_answer(
            _answer(TextBlock(text="  "), TextBlock(text="hi")),
            {},
            footer_text="1 tool calls",
        )
        assert _texts(view) == ["hi", "-# 1 tool calls"]

    def test_overlong_text_fits_and_full_text_is_attached(self) -> None:
        blocks = [TextBlock(text="x" * 3000), TextBlock(text="y" * 3000)]
        view, attached = render_answer(_answer(*blocks), {}, footer_text="1 tool calls")

        assert sum(len(t) for t in _texts(view)) <= MAX_MESSAGE_TEXT
        assert _texts(view)[1].endswith("…")
        [full] = attached
        assert full.filename == FULL_ANSWER_FILE
        assert full.fp.read().decode() == "x" * 3000 + "\n\n" + "y" * 3000

    def test_short_answer_has_no_full_text_file(self) -> None:
        _, attached = render_answer(_answer(), {}, footer_text="1 tool calls")
        assert attached == []


class TestUsageFooter:
    def test_tokens_and_cache_share(self) -> None:
        usage = Usage(
            tool_calls=26,
            input_tokens=40_000,
            cached_tokens=30_000,
            output_tokens=2_000,
        )
        assert usage_footer(usage, "unpriced-model") == (
            "26 tool calls · 42k tokens · 75% cached"
        )

    def test_cost_for_priced_model(self) -> None:
        usage = Usage(
            tool_calls=26,
            input_tokens=40_000,
            cached_tokens=30_000,
            output_tokens=2_000,
        )
        # 10k uncached at $4/M, 30k cached at $0.20/M, 2k out at $20/M
        assert usage_footer(usage, "claude-opus-5-5").endswith(" · ≈$0.09")

    def test_tiny_cost(self) -> None:
        assert usage_footer(Usage(input_tokens=100), "claude-opus-5-5").endswith(
            " · <$0.01"
        )

    def test_single_tool_call(self) -> None:
        footer = usage_footer(Usage(tool_calls=1), "unpriced-model")
        assert footer.startswith("1 tool call ·")

    def test_without_usage(self) -> None:
        assert usage_footer(Usage(), "unpriced-model") == "0 tool calls · 0 tokens"


class TestOnMessage:
    @pytest.fixture
    def run_agent(self, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        async def answer(
            _llm: AnthropicProvider,
            _system: str,
            _turns: list[Turn],
            tools: list[AgentTool],
            _schema: type[Answer],
            **_kwargs: Any,
        ) -> AgentResult[Answer]:
            save = next(t for t in tools if t.name == "save_file")
            await save.run(FileInput(filename="nodes.csv", content="a,b"))
            return AgentResult(_answer(attachments=["nodes.csv"]), Usage(tool_calls=2))

        run_agent = AsyncMock(side_effect=answer)
        monkeypatch.setattr(ask_module, "run_agent", run_agent)
        return run_agent

    async def test_answers_with_layout_and_files(
        self, cog: Ask, run_agent: AsyncMock
    ) -> None:
        message = _message(f"<@{BOT_ID}> list nodes")
        await cog.on_message(message)

        run_agent.assert_awaited_once()
        web_tools = {t["name"] for t in run_agent.call_args.kwargs["server_tools"]}
        assert web_tools == {"web_search", "web_fetch"}
        assert run_agent.call_args.kwargs["code_execution"] is True
        # the agent wraps up before the hard timeout cuts it off
        assert run_agent.call_args.kwargs["time_limit"] < ask_module.ANSWER_TIMEOUT
        kwargs = message.reply.call_args.kwargs
        assert "There are **4,000** nodes." in _texts(kwargs["view"])
        assert [f.filename for f in kwargs["files"]] == ["nodes.csv"]
        assert kwargs["mention_author"] is False

    async def test_reply_to_answer_is_a_follow_up(
        self, cog: Ask, run_agent: AsyncMock
    ) -> None:
        message = _message(f"<@{BOT_ID}> list nodes")
        await cog.on_message(message)
        answer = _message(message_id=message.reply.return_value.id, author_id=BOT_ID)

        await cog.on_message(
            _message("only active ones", message_id=200, parent=answer)
        )

        assert run_agent.await_count == 2

    async def test_ignores_non_questions(self, cog: Ask, run_agent: AsyncMock) -> None:
        message = _message("just chatting")
        await cog.on_message(message)
        run_agent.assert_not_awaited()
        message.add_reaction.assert_not_awaited()

    async def test_shrugs_at_strangers(self, cog: Ask, run_agent: AsyncMock) -> None:
        message = _message(f"<@{BOT_ID}> how many nodes?", author_id=STRANGER_ID)
        await cog.on_message(message)
        run_agent.assert_not_awaited()
        message.reply.assert_not_awaited()
        message.add_reaction.assert_awaited_once_with("\N{SHRUG}")

    @pytest.mark.parametrize(
        ("content", "is_bot", "in_guild"),
        [
            (f"<@{BOT_ID}> hi", True, True),  # other bots
            ("hi", False, False),  # stranger DMs
            ("hi", False, True),  # no mention
        ],
    )
    async def test_no_shrug_without_a_human_mention(
        self,
        cog: Ask,
        run_agent: AsyncMock,
        content: str,
        is_bot: bool,
        in_guild: bool,
    ) -> None:
        message = _message(
            content, author_id=STRANGER_ID, is_bot=is_bot, in_guild=in_guild
        )
        await cog.on_message(message)
        message.add_reaction.assert_not_awaited()

    async def test_failed_shrug_is_ignored(
        self, cog: Ask, run_agent: AsyncMock
    ) -> None:
        message = _message(f"<@{BOT_ID}> hi", author_id=STRANGER_ID)
        message.add_reaction.side_effect = discord.Forbidden(
            MagicMock(status=403), "no reactions"
        )
        await cog.on_message(message)
        cog.bot.report_error.assert_not_awaited()

    async def test_slow_answer_times_out(
        self, cog: Ask, run_agent: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def stuck(*_a: Any, usage: Usage, **_k: Any) -> None:
            usage.tool_calls = 3
            await asyncio.sleep(60)

        run_agent.side_effect = stuck
        monkeypatch.setattr(ask_module, "ANSWER_TIMEOUT", 0.01)
        message = _message(f"<@{BOT_ID}> list nodes")

        await cog.on_message(message)

        embed = message.reply.call_args.kwargs["embed"]
        assert embed.title == "That took too long"
        # what the abandoned run used is still shown
        assert embed.footer.text.startswith("3 tool calls")
        cog.bot.report_error.assert_not_awaited()
        assert message.reply.return_value.id not in cog._answers

    async def test_failure_is_reported(self, cog: Ask, run_agent: AsyncMock) -> None:
        async def fail(*_a: Any, usage: Usage, **_k: Any) -> None:
            usage.tool_calls = 4
            raise RuntimeError("node down")

        run_agent.side_effect = fail
        message = _message(f"<@{BOT_ID}> list nodes")

        await cog.on_message(message)

        cog.bot.report_error.assert_awaited_once()
        embed = message.reply.call_args.kwargs["embed"]
        assert "node down" in embed.description
        assert embed.footer.text.startswith("4 tool calls")
        assert message.reply.return_value.id not in cog._answers

    async def test_rejected_reply_falls_back_to_error(
        self, cog: Ask, run_agent: AsyncMock
    ) -> None:
        message = _message(f"<@{BOT_ID}> list nodes")
        message.reply.side_effect = [
            discord.HTTPException(MagicMock(status=400), "invalid layout"),
            MagicMock(id=5000),
        ]

        await cog.on_message(message)

        cog.bot.report_error.assert_awaited_once()
        assert "invalid layout" in message.reply.call_args.kwargs["embed"].description
        assert 5000 not in cog._answers


class TestSetup:
    async def test_loads_with_anthropic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cfg._instance.ask,
            "llm",
            LLMConfig(provider="anthropic", api_key="k", model="m"),
        )
        bot = make_bot()
        bot.add_cog = AsyncMock()
        await setup(bot)
        bot.add_cog.assert_awaited_once()

    @pytest.mark.parametrize(
        "llm",
        [LLMConfig(), LLMConfig(provider="openai", api_key="k", model="m")],
    )
    async def test_skips_without_anthropic(
        self, monkeypatch: pytest.MonkeyPatch, llm: LLMConfig
    ) -> None:
        monkeypatch.setattr(cfg._instance.ask, "llm", llm)
        bot = make_bot()
        bot.add_cog = AsyncMock()
        await setup(bot)
        bot.add_cog.assert_not_awaited()
