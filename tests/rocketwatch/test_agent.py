import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic import omit
from anthropic.types.beta import (
    BetaBashCodeExecutionOutputBlock,
    BetaBashCodeExecutionResultBlock,
    BetaBashCodeExecutionToolResultBlock,
    BetaCodeExecutionOutputBlock,
    BetaCodeExecutionResultBlock,
    BetaCodeExecutionToolResultBlock,
    BetaCodeExecutionToolResultError,
    BetaContainer,
    BetaFileMetadata,
    BetaServerToolCaller20260120,
    BetaServerToolUsage,
    BetaServerToolUseBlock,
    BetaTextBlock,
    BetaToolUseBlock,
    BetaWebFetchToolResultBlock,
)
from pydantic import BaseModel

from rocketwatch.utils import agent
from rocketwatch.utils.agent import (
    AgentError,
    AgentTool,
    ToolError,
    ToolOutput,
    Turn,
    Usage,
    called_from_code,
    request_cost,
    run_agent,
)
from rocketwatch.utils.llm import AnthropicProvider, ImageInput


class Reply(BaseModel):
    answer: str


class EchoInput(BaseModel):
    value: int


def _response(stop_reason: str, *blocks: Any) -> MagicMock:
    response = MagicMock()
    response.stop_reason = stop_reason
    response.content = list(blocks)
    response.usage.input_tokens = 10
    response.usage.cache_read_input_tokens = 0
    response.usage.output_tokens = 5
    response.usage.cache_creation_input_tokens = 0
    response.usage.server_tool_use = None
    response.container = None
    response.model = "claude-test"
    return response


def _answer(text: str) -> MagicMock:
    return _response(
        "end_turn",
        BetaTextBlock(type="text", text=Reply(answer=text).model_dump_json()),
    )


def _tool_use(*calls: tuple[str, str, dict[str, Any]]) -> MagicMock:
    return _response(
        "tool_use",
        *(
            BetaToolUseBlock(type="tool_use", id=call_id, name=name, input=args)
            for call_id, name, args in calls
        ),
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr(AnthropicProvider, "_get_client", lambda self: client)
    return client


@pytest.fixture
def create(client: MagicMock) -> AsyncMock:
    client.beta.messages.create = create = AsyncMock()
    return create


@pytest.fixture
def stored(client: MagicMock) -> dict[str, tuple[str, bytes]]:
    """Files the Files API holds, by id, as (filename, content)."""
    stored: dict[str, tuple[str, bytes]] = {}

    async def metadata(file_id: str, **_: Any) -> BetaFileMetadata:
        name, data = stored[file_id]
        return BetaFileMetadata(
            id=file_id,
            filename=name,
            mime_type="application/octet-stream",
            size_bytes=len(data),
            created_at=datetime(2026, 10, 25, tzinfo=UTC),
            type="file",
        )

    async def download(file_id: str, **_: Any) -> MagicMock:
        response = MagicMock()
        response.read = AsyncMock(return_value=stored[file_id][1])
        return response

    async def delete(file_id: str, **_: Any) -> None:
        del stored[file_id]

    async def upload(file: tuple[str, bytes], **_: Any) -> MagicMock:
        uploaded = MagicMock()
        uploaded.id = f"up{len(stored)}"
        stored[uploaded.id] = file
        return uploaded

    client.beta.files.retrieve_metadata = AsyncMock(side_effect=metadata)
    client.beta.files.download = AsyncMock(side_effect=download)
    client.beta.files.delete = AsyncMock(side_effect=delete)
    client.beta.files.upload = AsyncMock(side_effect=upload)
    return stored


@pytest.fixture
def provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="key", model="claude-test")


def _echo_tool(run: AsyncMock | None = None) -> AgentTool:
    return AgentTool(
        "echo", "Echo a value", EchoInput, run or AsyncMock(return_value="echoed")
    )


def _tool_results(create: AsyncMock) -> list[list[dict[str, Any]]]:
    """Tool results sent back to the model, grouped by message."""
    messages = create.call_args.kwargs["messages"]
    groups = [
        [b for b in m["content"] if b.get("type") == "tool_result"]
        for m in messages
        if m["role"] == "user" and isinstance(m["content"], list)
    ]
    return [group for group in groups if group]


def _notes(create: AsyncMock) -> list[str]:
    """Text the loop added next to tool results."""
    messages = create.call_args.kwargs["messages"]
    return [
        b["text"]
        for m in messages
        if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "text"
    ]


async def test_returns_parsed_answer(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    create.return_value = _answer("42")
    result = await run_agent(provider, "sys", [Turn("user", "q")], [], Reply)
    assert result.output == Reply(answer="42")
    assert result.usage.tool_calls == 0


async def test_usage_is_summed_over_requests(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    first = _tool_use(("t1", "echo", {"value": 1}))
    first.usage.cache_creation_input_tokens = 100
    second = _answer("done")
    second.usage.cache_read_input_tokens = 90
    create.side_effect = [first, second]

    result = await run_agent(
        provider, "sys", [Turn("user", "q")], [_echo_tool()], Reply
    )

    # input counts uncached, cache writes and cache reads alike
    assert result.usage.input_tokens == (10 + 100) + (10 + 90)
    assert result.usage.cached_tokens == 90
    assert result.usage.cache_write_tokens == 100
    assert result.usage.output_tokens == 10


async def test_web_searches_are_counted(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    answer = _answer("done")
    answer.usage.server_tool_use = BetaServerToolUsage(
        web_search_requests=3, web_fetch_requests=2
    )
    create.return_value = answer
    result = await run_agent(provider, "sys", [Turn("user", "q")], [], Reply)
    assert result.usage.web_searches == 3


async def test_usage_survives_a_failed_run(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    create.side_effect = [_tool_use(("t1", "echo", {"value": 1})), RuntimeError()]
    usage = Usage()
    with pytest.raises(RuntimeError):
        await run_agent(
            provider, "sys", [Turn("user", "q")], [_echo_tool()], Reply, usage=usage
        )
    assert usage.tool_calls == 1
    assert usage.output_tokens == 5


def test_request_cost() -> None:
    # Opus 5.5: 100k uncached at $4, 800k reads at $0.20, 200k writes at $5,
    # 50k output at $20 per million, plus 4 searches at a cent
    cost = request_cost("claude-opus-5-5", 100_000, 800_000, 200_000, 50_000, 4)
    assert cost == pytest.approx(0.4 + 0.16 + 1.0 + 1.0 + 0.04)


def test_haiku_charges_more_for_long_prompts() -> None:
    # up to 100k prompt tokens: $0.10 in, $0.01 cached, $0.50 out per million
    short = request_cost("claude-haiku-5-5", 10_000, 80_000, 0, 1_000)
    assert short == pytest.approx((1_000 + 800 + 500) / 1e6)
    # beyond: $0.50 in, $0.05 cached, $2.50 out
    long = request_cost("claude-haiku-5-5", 10_000, 100_000, 0, 1_000)
    assert long == pytest.approx((5_000 + 5_000 + 2_500) / 1e6)


def test_request_cost_unknown_model() -> None:
    assert request_cost("some-other-model", 10, 0, 0, 0) is None


async def test_cost_adds_up_per_request(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    first, second = _tool_use(("t1", "echo", {"value": 1})), _answer("done")
    first.model = second.model = "claude-opus-5-5"
    create.side_effect = [first, second]
    result = await run_agent(
        provider, "sys", [Turn("user", "q")], [_echo_tool()], Reply
    )
    # each: 10 input at $4 and 5 output at $20 per million
    assert result.usage.cost == pytest.approx(2 * (10 * 4 + 5 * 20) / 1e6)


async def test_cost_unknown_for_unpriced_model(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    create.return_value = _answer("done")
    result = await run_agent(provider, "sys", [Turn("user", "q")], [], Reply)
    assert result.usage.cost is None


async def test_request_uses_model_system_and_turns(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    create.return_value = _answer("ok")
    image = ImageInput(data=b"\x89PNG", media_type="image/png")
    turns = [
        Turn("user", "first", [image]),
        Turn("assistant", "earlier answer"),
        Turn("user", "follow-up"),
    ]
    await run_agent(provider, "the system prompt", turns, [_echo_tool()], Reply)

    kwargs = create.call_args.kwargs
    assert kwargs["model"] == "claude-test"
    assert kwargs["system"][0]["text"] == "the system prompt"
    assert [t["name"] for t in kwargs["tools"]] == ["echo"]
    messages = kwargs["messages"]
    assert [m["role"] for m in messages[:3]] == ["user", "assistant", "user"]
    assert messages[0]["content"][0]["type"] == "image"
    assert messages[2]["content"] == "follow-up"


async def test_runs_tool_and_returns_result_to_model(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    run = AsyncMock(return_value="tool output")
    create.side_effect = [_tool_use(("t1", "echo", {"value": 7})), _answer("done")]

    result = await run_agent(
        provider, "sys", [Turn("user", "q")], [_echo_tool(run)], Reply
    )

    assert result.output.answer == "done"
    assert result.usage.tool_calls == 1
    run.assert_awaited_once_with(EchoInput(value=7))
    [[tool_result]] = _tool_results(create)
    assert tool_result["tool_use_id"] == "t1"
    assert tool_result["content"] == "tool output"
    assert not tool_result["is_error"]


async def test_parallel_calls_answered_in_one_message(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    create.side_effect = [
        _tool_use(("a", "echo", {"value": 1}), ("b", "echo", {"value": 2})),
        _answer("done"),
    ]
    result = await run_agent(
        provider, "sys", [Turn("user", "q")], [_echo_tool()], Reply
    )

    assert result.usage.tool_calls == 2
    [results] = _tool_results(create)
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


@pytest.mark.parametrize(
    ("tool_name", "args", "error", "expected"),
    [
        ("echo", {"value": 1}, ToolError("no such node"), "no such node"),
        ("echo", {"value": 1}, RuntimeError("boom"), "RuntimeError: boom"),
        ("echo", {"value": "not a number"}, None, "Invalid input"),
        ("missing", {}, None, "Unknown tool"),
    ],
)
async def test_tool_failures_are_reported_to_model(
    provider: AnthropicProvider,
    create: AsyncMock,
    tool_name: str,
    args: dict[str, Any],
    error: Exception | None,
    expected: str,
) -> None:
    run = AsyncMock(side_effect=error)
    create.side_effect = [_tool_use(("t1", tool_name, args)), _answer("recovered")]

    result = await run_agent(
        provider, "sys", [Turn("user", "q")], [_echo_tool(run)], Reply
    )

    assert result.output.answer == "recovered"
    [[tool_result]] = _tool_results(create)
    assert tool_result["is_error"]
    assert expected in tool_result["content"]


async def test_tool_images_reach_the_model(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    image = ImageInput(b"\x89PNG", "image/png")
    run = AsyncMock(return_value=ToolOutput("a chart", [image]))
    create.side_effect = [_tool_use(("t1", "echo", {"value": 1})), _answer("seen")]

    await run_agent(provider, "sys", [Turn("user", "q")], [_echo_tool(run)], Reply)

    [[tool_result]] = _tool_results(create)
    text, picture = tool_result["content"]
    assert text == {"type": "text", "text": "a chart"}
    assert picture["type"] == "image"


async def test_scripts_get_only_text(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    image = ImageInput(b"\x89PNG", "image/png")
    run = AsyncMock(return_value=ToolOutput("a chart", [image]))
    start = BetaServerToolUseBlock(
        type="server_tool_use", id="srv1", name="code_execution", input={"code": "..."}
    )
    create.side_effect = [
        _response("tool_use", start, _script_call("t0", 0)),
        _answer("done"),
    ]

    await run_agent(
        provider,
        "sys",
        [Turn("user", "q")],
        [_echo_tool(run)],
        Reply,
        code_execution=True,
    )

    [[tool_result]] = _tool_results(create)
    assert tool_result["content"].startswith("a chart")
    assert "left out" in tool_result["content"]


async def test_slow_tool_is_stopped(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    async def hang(args: EchoInput) -> str:
        await asyncio.sleep(60)
        return "never"

    create.side_effect = [_tool_use(("t1", "echo", {"value": 1})), _answer("narrowed")]

    result = await run_agent(
        provider,
        "sys",
        [Turn("user", "q")],
        [_echo_tool(AsyncMock(side_effect=hang))],
        Reply,
        tool_timeout=0.05,
    )

    assert result.output.answer == "narrowed"
    [[tool_result]] = _tool_results(create)
    assert tool_result["is_error"]
    assert "Stopped after" in tool_result["content"]


async def test_forces_answer_after_max_iterations(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    create.side_effect = [_tool_use(("t1", "echo", {"value": 1})), _answer("final")]

    result = await run_agent(
        provider, "sys", [Turn("user", "q")], [_echo_tool()], Reply, max_iterations=1
    )

    assert result.output.answer == "final"
    first, last = create.call_args_list
    assert first.kwargs["tool_choice"] != {"type": "none"}
    assert last.kwargs["tool_choice"] == {"type": "none"}
    # the model learns why, so a cut-short answer can say what's missing
    [note] = _notes(create)
    assert "say what you couldn't check" in note


async def test_pause_turn_continues(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    create.side_effect = [_response("pause_turn"), _answer("resumed")]
    result = await run_agent(provider, "sys", [Turn("user", "q")], [], Reply)
    assert result.output.answer == "resumed"


async def test_server_tools_are_sent_and_counted(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    search = {"type": "web_search_20260209", "name": "web_search"}
    server_use = BetaServerToolUseBlock(
        type="server_tool_use", id="s1", name="web_search", input={"query": "RPIP-42"}
    )
    answer = _answer("found it")
    answer.content.insert(0, server_use)
    create.return_value = answer

    result = await run_agent(
        provider,
        "sys",
        [Turn("user", "q")],
        [_echo_tool()],
        Reply,
        server_tools=[search],
    )

    assert result.output.answer == "found it"
    assert result.usage.tool_calls == 1
    assert create.call_args.kwargs["tools"][-1] == search


def _script_call(call_id: str, value: int) -> BetaToolUseBlock:
    """A tool call made by a script running in code execution."""
    return BetaToolUseBlock(
        type="tool_use",
        id=call_id,
        name="echo",
        input={"value": value},
        caller=BetaServerToolCaller20260120(
            type="code_execution_20260120", tool_id="srv1"
        ),
    )


def _with_container(response: MagicMock, container_id: str) -> MagicMock:
    response.container = BetaContainer(
        id=container_id, expires_at="2026-10-23T14:00:00Z"
    )
    return response


async def test_code_execution_offers_tools_to_scripts(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    create.return_value = _answer("ok")
    await run_agent(
        provider,
        "sys",
        [Turn("user", "q")],
        [_echo_tool()],
        Reply,
        code_execution=True,
    )
    tools = create.call_args.kwargs["tools"]
    assert tools[0]["allowed_callers"] == ["direct", "code_execution_20260120"]
    assert {"type": "code_execution_20260120", "name": "code_execution"} in tools


async def test_tools_are_direct_only_without_code_execution(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    create.return_value = _answer("ok")
    await run_agent(provider, "sys", [Turn("user", "q")], [_echo_tool()], Reply)
    [tool] = create.call_args.kwargs["tools"]
    assert "allowed_callers" not in tool


async def test_tools_know_who_called_them(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    seen: dict[int, bool] = {}

    async def record(args: EchoInput) -> str:
        seen[args.value] = called_from_code()
        return "ok"

    start = BetaServerToolUseBlock(
        type="server_tool_use", id="srv1", name="code_execution", input={"code": "..."}
    )
    create.side_effect = [
        _response(
            "tool_use",
            start,
            BetaToolUseBlock(type="tool_use", id="d", name="echo", input={"value": 1}),
            _script_call("s", 2),
        ),
        _answer("done"),
    ]

    await run_agent(
        provider,
        "sys",
        [Turn("user", "q")],
        [_echo_tool(AsyncMock(side_effect=record))],
        Reply,
        code_execution=True,
    )

    assert seen == {1: False, 2: True}


async def test_container_is_reused(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    start = BetaServerToolUseBlock(
        type="server_tool_use", id="srv1", name="code_execution", input={"code": "..."}
    )
    first = _with_container(_response("tool_use", start, _script_call("t1", 1)), "c1")
    create.side_effect = [first, _with_container(_answer("done"), "c1")]

    result = await run_agent(
        provider,
        "sys",
        [Turn("user", "q")],
        [_echo_tool()],
        Reply,
        code_execution=True,
    )

    assert result.output.answer == "done"
    first_call, second_call = create.call_args_list
    assert first_call.kwargs["container"] is omit
    assert second_call.kwargs["container"] == "c1"


async def test_script_round_trips_dont_use_up_turns(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    # a script calling a tool five times in a row, with a one-turn budget
    start = BetaServerToolUseBlock(
        type="server_tool_use", id="srv1", name="code_execution", input={"code": "..."}
    )
    create.side_effect = [
        _response("tool_use", start, _script_call("t0", 0)),
        *(_response("tool_use", _script_call(f"t{i}", i)) for i in range(1, 5)),
        _answer("summed"),
    ]

    result = await run_agent(
        provider,
        "sys",
        [Turn("user", "q")],
        [_echo_tool()],
        Reply,
        code_execution=True,
        max_iterations=1,
    )

    assert result.output.answer == "summed"
    assert create.await_count == 6
    # never forced to answer while the script still waits for results
    assert all(c.kwargs["tool_choice"] is omit for c in create.call_args_list)


async def test_forced_answer_waits_for_script(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    start = BetaServerToolUseBlock(
        type="server_tool_use", id="srv1", name="code_execution", input={"code": "..."}
    )
    model_turn = _tool_use(("t1", "echo", {"value": 1}))
    model_turn.content.insert(0, BetaTextBlock(type="text", text="script finished"))
    create.side_effect = [
        _response("tool_use", start, _script_call("t0", 0)),
        model_turn,
        _answer("final"),
    ]

    result = await run_agent(
        provider,
        "sys",
        [Turn("user", "q")],
        [_echo_tool()],
        Reply,
        code_execution=True,
        max_iterations=1,
    )

    assert result.output.answer == "final"
    choices = [c.kwargs["tool_choice"] for c in create.call_args_list]
    assert choices[:2] == [omit, omit]
    assert choices[2] == {"type": "none"}


async def test_forced_answer_waits_for_deferred_server_tool(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    # a web fetch called next to a client tool only runs once the tool results
    # come back alone; anything added to them fails the request
    fetch = BetaServerToolUseBlock(
        type="server_tool_use", id="srv1", name="web_fetch", input={"url": "x"}
    )
    fetched = BetaWebFetchToolResultBlock.model_construct(
        type="web_fetch_tool_result", tool_use_id="srv1", content=None
    )
    both = _tool_use(("t1", "echo", {"value": 1}))
    both.content.insert(0, fetch)
    after_fetch = _tool_use(("t2", "echo", {"value": 2}))
    after_fetch.content.insert(0, fetched)
    create.side_effect = [both, after_fetch, _answer("final")]

    result = await run_agent(
        provider, "sys", [Turn("user", "q")], [_echo_tool()], Reply, max_iterations=1
    )

    assert result.output.answer == "final"
    choices = [c.kwargs["tool_choice"] for c in create.call_args_list]
    assert choices == [omit, omit, {"type": "none"}]
    messages = create.call_args.kwargs["messages"]
    assert all(b["type"] == "tool_result" for b in messages[2]["content"])
    assert len(_notes(create)) == 1


async def test_paused_turn_is_resumed_before_forcing(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    search = BetaServerToolUseBlock(
        type="server_tool_use", id="srv1", name="web_search", input={"query": "x"}
    )
    create.side_effect = [_response("pause_turn", search), _answer("resumed")]

    result = await run_agent(
        provider, "sys", [Turn("user", "q")], [], Reply, max_iterations=1
    )

    assert result.output.answer == "resumed"
    assert all(c.kwargs["tool_choice"] is omit for c in create.call_args_list)


async def test_script_is_stopped_when_requests_run_out(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    start = BetaServerToolUseBlock(
        type="server_tool_use", id="srv1", name="code_execution", input={"code": "..."}
    )
    model_turn = _tool_use(("d", "echo", {"value": 9}))
    model_turn.content.insert(0, BetaTextBlock(type="text", text="script failed"))
    create.side_effect = [
        _response("tool_use", start, _script_call("t0", 0)),
        _response("tool_use", _script_call("t1", 1)),
        _response("tool_use", _script_call("t2", 2)),
        model_turn,
        _answer("partial"),
    ]
    run = AsyncMock(return_value="echoed")

    result = await run_agent(
        provider,
        "sys",
        [Turn("user", "q")],
        [_echo_tool(run)],
        Reply,
        code_execution=True,
        max_requests=2,
    )

    assert result.output.answer == "partial"
    assert [c.args[0].value for c in run.await_args_list] == [0, 1, 9]
    stopped = _tool_results(create)[2][0]
    assert stopped["is_error"]
    assert "Stop the script" in stopped["content"]
    assert create.call_args.kwargs["tool_choice"] == {"type": "none"}
    assert len(_notes(create)) == 1


async def test_script_is_stopped_after_time_limit(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    async def slow(args: EchoInput) -> str:
        await asyncio.sleep(0.1)
        return "echoed"

    start = BetaServerToolUseBlock(
        type="server_tool_use", id="srv1", name="code_execution", input={"code": "..."}
    )
    create.side_effect = [
        _response("tool_use", start, _script_call("t0", 0)),
        _response("tool_use", _script_call("t1", 1)),
        _answer("partial"),
    ]
    run = AsyncMock(side_effect=slow)

    result = await run_agent(
        provider,
        "sys",
        [Turn("user", "q")],
        [_echo_tool(run)],
        Reply,
        code_execution=True,
        time_limit=0.05,
    )

    assert result.output.answer == "partial"
    run.assert_awaited_once()
    assert _tool_results(create)[1][0]["is_error"]


async def test_script_that_ignores_stop_gives_up(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    create.return_value = _response("tool_use", _script_call("t", 0))
    run = AsyncMock(return_value="echoed")

    with pytest.raises(AgentError, match="No answer"):
        await run_agent(
            provider,
            "sys",
            [Turn("user", "q")],
            [_echo_tool(run)],
            Reply,
            code_execution=True,
            max_requests=1,
        )

    run.assert_awaited_once()


async def test_tool_calls_after_forced_answer_fail(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    create.side_effect = [
        _tool_use(("t1", "echo", {"value": 1})),
        _tool_use(("t2", "echo", {"value": 2})),
    ]
    with pytest.raises(AgentError, match="told to answer"):
        await run_agent(
            provider,
            "sys",
            [Turn("user", "q")],
            [_echo_tool()],
            Reply,
            max_iterations=1,
        )


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
async def test_early_stop_raises(
    provider: AnthropicProvider, create: AsyncMock, stop_reason: str
) -> None:
    create.return_value = _response(stop_reason)
    with pytest.raises(AgentError, match=stop_reason):
        await run_agent(provider, "sys", [Turn("user", "q")], [], Reply)


def _script_run(*file_ids: str) -> BetaCodeExecutionToolResultBlock:
    """A finished script that saved files to $OUTPUT_DIR."""
    return BetaCodeExecutionToolResultBlock(
        type="code_execution_tool_result",
        tool_use_id="srv1",
        content=BetaCodeExecutionResultBlock(
            type="code_execution_result",
            content=[
                BetaCodeExecutionOutputBlock(type="code_execution_output", file_id=i)
                for i in file_ids
            ],
            return_code=0,
            stdout="",
            stderr="",
        ),
    )


def _bash_run(*file_ids: str) -> BetaBashCodeExecutionToolResultBlock:
    return BetaBashCodeExecutionToolResultBlock(
        type="bash_code_execution_tool_result",
        tool_use_id="srv2",
        content=BetaBashCodeExecutionResultBlock(
            type="bash_code_execution_result",
            content=[
                BetaBashCodeExecutionOutputBlock(
                    type="bash_code_execution_output", file_id=i
                )
                for i in file_ids
            ],
            return_code=0,
            stdout="",
            stderr="",
        ),
    )


def _with_blocks(response: MagicMock, *blocks: Any) -> MagicMock:
    response.content[:0] = blocks
    return response


async def test_exported_files_are_returned_and_deleted(
    provider: AnthropicProvider,
    create: AsyncMock,
    stored: dict[str, tuple[str, bytes]],
) -> None:
    stored.update(
        {
            "f1": ("pie.png", b"old"),
            "f2": ("nodes.csv", b"a,b"),
            "f3": ("pie.png", b"new"),
            "f4": ("hist.png", b"png"),
        }
    )
    create.side_effect = [
        _with_blocks(_response("pause_turn"), _script_run("f1", "f2")),
        _with_blocks(_answer("done"), _script_run("f3"), _bash_run("f4")),
    ]

    result = await run_agent(
        provider, "sys", [Turn("user", "q")], [], Reply, code_execution=True
    )

    # a rerun's export replaces the earlier one under the same name
    assert result.files == {"pie.png": b"new", "nodes.csv": b"a,b", "hist.png": b"png"}
    assert stored == {}


async def test_oversized_exports_are_left_out(
    provider: AnthropicProvider,
    create: AsyncMock,
    stored: dict[str, tuple[str, bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent, "MAX_EXPORT_BYTES", 4)
    stored.update({"f1": ("big.png", b"12345"), "f2": ("small.png", b"1234")})
    create.return_value = _with_blocks(_answer("done"), _script_run("f1", "f2"))

    result = await run_agent(
        provider, "sys", [Turn("user", "q")], [], Reply, code_execution=True
    )

    assert result.files == {"small.png": b"1234"}
    assert stored == {}


async def test_failed_script_exports_nothing(
    provider: AnthropicProvider,
    create: AsyncMock,
    stored: dict[str, tuple[str, bytes]],
) -> None:
    failed = BetaCodeExecutionToolResultBlock(
        type="code_execution_tool_result",
        tool_use_id="srv1",
        content=BetaCodeExecutionToolResultError(
            type="code_execution_tool_result_error", error_code="unavailable"
        ),
    )
    create.return_value = _with_blocks(_answer("done"), failed)

    result = await run_agent(
        provider, "sys", [Turn("user", "q")], [], Reply, code_execution=True
    )

    assert result.output.answer == "done"
    assert result.files == {}


async def test_exports_are_deleted_when_the_run_fails(
    provider: AnthropicProvider,
    create: AsyncMock,
    stored: dict[str, tuple[str, bytes]],
) -> None:
    stored["f1"] = ("pie.png", b"png")
    create.side_effect = [
        _with_blocks(_response("pause_turn"), _script_run("f1")),
        _response("refusal"),
    ]

    with pytest.raises(AgentError):
        await run_agent(
            provider, "sys", [Turn("user", "q")], [], Reply, code_execution=True
        )

    assert stored == {}


async def test_failed_download_still_answers(
    provider: AnthropicProvider,
    create: AsyncMock,
    client: MagicMock,
    stored: dict[str, tuple[str, bytes]],
) -> None:
    stored["f1"] = ("pie.png", b"png")
    client.beta.files.download.side_effect = RuntimeError("files api down")
    create.return_value = _with_blocks(_answer("done"), _script_run("f1"))

    result = await run_agent(
        provider, "sys", [Turn("user", "q")], [], Reply, code_execution=True
    )

    assert result.output.answer == "done"
    assert result.files == {}
    assert stored == {}


async def test_container_files_reach_the_sandbox_and_are_deleted(
    provider: AnthropicProvider,
    create: AsyncMock,
    stored: dict[str, tuple[str, bytes]],
) -> None:
    seen: list[dict[str, Any]] = []

    async def answer(**kwargs: Any) -> MagicMock:
        seen.append(kwargs["messages"][0])
        assert set(stored) == {"up0"}
        return _answer("done")

    create.side_effect = answer

    await run_agent(
        provider,
        "sys",
        [Turn("user", "first"), Turn("assistant", "a"), Turn("user", "q")],
        [],
        Reply,
        code_execution=True,
        container_files=[("style.mplstyle", b"axes.grid: True")],
    )

    [first] = seen
    assert first["content"] == [
        {"type": "container_upload", "file_id": "up0"},
        {"type": "text", "text": "first"},
    ]
    assert stored == {}


async def test_container_files_are_deleted_when_the_run_fails(
    provider: AnthropicProvider,
    create: AsyncMock,
    stored: dict[str, tuple[str, bytes]],
) -> None:
    create.return_value = _response("refusal")
    with pytest.raises(AgentError):
        await run_agent(
            provider,
            "sys",
            [Turn("user", "q")],
            [],
            Reply,
            code_execution=True,
            container_files=[("style.mplstyle", b"")],
        )
    assert stored == {}
