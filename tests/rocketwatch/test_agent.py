from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic import omit
from anthropic.types.beta import (
    BetaContainer,
    BetaServerToolCaller20260120,
    BetaServerToolUsage,
    BetaServerToolUseBlock,
    BetaTextBlock,
    BetaToolUseBlock,
)
from pydantic import BaseModel

from rocketwatch.utils.agent import (
    AgentError,
    AgentResult,
    AgentTool,
    ToolError,
    Turn,
    estimate_cost,
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
def create(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    create = AsyncMock()
    client = MagicMock()
    client.beta.messages.create = create
    monkeypatch.setattr(AnthropicProvider, "_get_client", lambda self: client)
    return create


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
    return [
        list(m["content"])
        for m in messages
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and all(block.get("type") == "tool_result" for block in m["content"])
    ]


async def test_returns_parsed_answer(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    create.return_value = _answer("42")
    result = await run_agent(provider, "sys", [Turn("user", "q")], [], Reply)
    assert result.output == Reply(answer="42")
    assert result.tool_calls == 0


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
    assert result.input_tokens == (10 + 100) + (10 + 90)
    assert result.cached_tokens == 90
    assert result.cache_write_tokens == 100
    assert result.output_tokens == 10


async def test_web_searches_are_counted(
    provider: AnthropicProvider, create: AsyncMock
) -> None:
    answer = _answer("done")
    answer.usage.server_tool_use = BetaServerToolUsage(
        web_search_requests=3, web_fetch_requests=2
    )
    create.return_value = answer
    result = await run_agent(provider, "sys", [Turn("user", "q")], [], Reply)
    assert result.web_searches == 3


def test_estimate_cost() -> None:
    result = AgentResult(
        Reply(answer="x"),
        tool_calls=5,
        input_tokens=1_100_000,
        cached_tokens=800_000,
        cache_write_tokens=200_000,
        output_tokens=50_000,
        web_searches=4,
    )
    # Opus 5.5: 100k uncached at $4, 800k reads at $0.20, 200k writes at $5,
    # 50k output at $20 per million, plus 4 searches at a cent
    assert estimate_cost(result, "claude-opus-5-5") == pytest.approx(
        0.4 + 0.16 + 1.0 + 1.0 + 0.04
    )


def test_estimate_cost_unknown_model() -> None:
    result = AgentResult(Reply(answer="x"), tool_calls=0, input_tokens=10)
    assert estimate_cost(result, "some-other-model") is None


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
    assert result.tool_calls == 1
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

    assert result.tool_calls == 2
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
    assert result.tool_calls == 1
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
