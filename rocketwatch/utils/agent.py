from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ValidationError

from rocketwatch.utils.llm import AnthropicProvider, ImageInput

if TYPE_CHECKING:
    from anthropic.types.beta import (
        BetaContentBlock,
        BetaContentBlockParam,
        BetaMessageParam,
        BetaOutputConfigParam,
        BetaToolChoiceParam,
        BetaToolParam,
        BetaToolResultBlockParam,
        BetaToolUnionParam,
        BetaToolUseBlock,
    )

log = logging.getLogger("rocketwatch.agent")

_called_from_code: ContextVar[bool] = ContextVar("called_from_code", default=False)


def called_from_code() -> bool:
    """Whether the running tool was called by a script in code execution, whose
    result goes back to the script rather than into the model's context."""
    return _called_from_code.get()


Effort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant"]
    text: str
    images: list[ImageInput] = field(default_factory=list)


class ToolError(Exception):
    """A tool failure whose message is reported back to the model."""


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    input_model: type[BaseModel]
    run: Callable[[Any], Awaitable[str]]


@dataclass
class Usage:
    tool_calls: int = 0
    # input_tokens includes cache reads and writes
    input_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0


@dataclass(frozen=True)
class AgentResult[R: BaseModel]:
    output: R
    usage: Usage


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float  # 5-minute cache, 1.25x input


# Anthropic's list prices as of October 2026; update when they change
PRICES = {
    "claude-opus-5-5": ModelPrice(4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": ModelPrice(2.00, 10.00, 0.20, 2.50),
    "claude-fable-5-1": ModelPrice(10.00, 50.00, 0.25, 12.50),
}
WEB_SEARCH_PRICE = 0.01


def estimate_cost(usage: Usage, model: str) -> float | None:
    """Approximate USD cost of a run, or None for a model without known prices."""
    if (price := PRICES.get(model)) is None:
        return None
    uncached = usage.input_tokens - usage.cached_tokens - usage.cache_write_tokens
    tokens = (
        uncached * price.input
        + usage.cached_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
        + usage.output_tokens * price.output
    )
    return tokens / 1_000_000 + usage.web_searches * WEB_SEARCH_PRICE


class AgentError(Exception):
    pass


def _build_message(turn: Turn) -> BetaMessageParam:
    content = AnthropicProvider._build_user_content(turn.text, turn.images)
    # each role has its own message type, so the role can't be a variable
    if turn.role == "user":
        return {"role": "user", "content": content}
    return {"role": "assistant", "content": content}


CODE_EXECUTION = "code_execution_20260120"
# requests allowed past the budget for a script to stop and the model to answer
WIND_DOWN_REQUESTS = 10
STOP_SCRIPT = (
    "Out of time for tool calls, so this one didn't run. Stop the script and "
    "answer with the data you have."
)
OUT_OF_TOOLS = (
    "No more tool calls are available. Answer now with the data you have, and "
    "say what you couldn't check."
)


def _tool_param(tool: AgentTool, from_code: bool) -> BetaToolParam:
    # plain pydantic schema: the strict transform would forbid free-form
    # objects like Mongo filters
    param: BetaToolParam = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_model.model_json_schema(),
    }
    if from_code:
        param["allowed_callers"] = ["direct", CODE_EXECUTION]
    return param


def _from_code(block: BetaContentBlock) -> bool:
    return (
        block.type == "tool_use"
        and block.caller is not None
        and block.caller.type != "direct"
    )


def _awaits_server_tool(content: list[BetaContentBlock]) -> bool:
    """Whether a server tool call has no result yet. Called alongside client
    tools, it runs once their results are sent, and only if nothing else is."""
    called = {b.id for b in content if b.type in ("server_tool_use", "mcp_tool_use")}
    answered = {getattr(b, "tool_use_id", None) for b in content}
    return bool(called - answered)


def _tool_result(
    block: BetaToolUseBlock, content: str, is_error: bool = False
) -> BetaToolResultBlockParam:
    return {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": content,
        "is_error": is_error,
    }


async def _run_tool(
    tools: dict[str, AgentTool], block: BetaToolUseBlock, timeout: float
) -> BetaToolResultBlockParam:
    def result(content: str, is_error: bool = False) -> BetaToolResultBlockParam:
        return _tool_result(block, content, is_error)

    if (tool := tools.get(block.name)) is None:
        return result(f"Unknown tool {block.name!r}", is_error=True)
    # each call runs in its own task, so this doesn't leak between calls
    _called_from_code.set(from_code := _from_code(block))
    try:
        args = tool.input_model.model_validate(block.input)
    except ValidationError as err:
        return result(f"Invalid input: {err}", is_error=True)
    loop = asyncio.get_running_loop()
    start = loop.time()
    try:
        async with asyncio.timeout(timeout):
            return result(await tool.run(args))
    except TimeoutError:
        return result(
            f"Stopped after {timeout:.0f}s. Ask for less, e.g. a smaller block "
            "range or fewer items.",
            is_error=True,
        )
    except ToolError as err:
        return result(str(err), is_error=True)
    except Exception as err:
        log.warning("Tool %s failed", block.name, exc_info=True)
        return result(f"{type(err).__name__}: {err}", is_error=True)
    finally:
        log.log(
            logging.DEBUG if from_code else logging.INFO,
            "Tool %s took %.1fs",
            block.name,
            loop.time() - start,
        )


async def run_agent[T: BaseModel](
    provider: AnthropicProvider,
    system: str,
    turns: list[Turn],
    tools: list[AgentTool],
    schema: type[T],
    *,
    server_tools: Sequence[BetaToolUnionParam] = (),
    code_execution: bool = False,
    max_iterations: int = 25,
    max_requests: int = 200,
    time_limit: float | None = None,
    tool_timeout: float = 30,
    effort: Effort = "high",
    max_tokens: int = 16000,
    usage: Usage | None = None,
) -> AgentResult[T]:
    """Let the model call tools until it produces an answer matching schema.

    server_tools are tools Anthropic runs itself, such as web search. With
    code_execution, the model can also run Python in Anthropic's sandbox and
    call the tools from there.

    max_iterations counts the model's own turns. Each tool call round trip from
    a running script is a request too, so max_requests and time_limit (seconds)
    bound those: past either, scripts get an error instead of tool results and
    the model has to answer. A tool call running past tool_timeout (seconds)
    is stopped and reported to the model as an error.

    Totals are added to usage as the run goes, so a caller that passes one in
    can still see what a failed or cancelled run used.
    """
    from anthropic import omit, transform_schema

    tools_by_name = {tool.name: tool for tool in tools}
    tool_params: list[BetaToolUnionParam] = [
        _tool_param(tool, code_execution) for tool in tools
    ]
    tool_params += server_tools
    if code_execution:
        tool_params.append({"type": CODE_EXECUTION, "name": "code_execution"})
    output_config: BetaOutputConfigParam = {
        "effort": effort,
        "format": {"type": "json_schema", "schema": transform_schema(schema)},
    }
    # out of iterations: make the model answer with what it has
    answer_now: BetaToolChoiceParam = {"type": "none"}
    messages = [_build_message(turn) for turn in turns]
    if usage is None:
        usage = Usage()
    model_turns = 0
    container: str | None = None
    script_waiting = False
    # the model's turn is still running (a paused turn, a script waiting on tool
    # results or a deferred server tool), so it can't be made to answer yet
    turn_open = False
    told = False
    loop = asyncio.get_running_loop()
    deadline = None if time_limit is None else loop.time() + time_limit

    for request in range(max_requests + WIND_DOWN_REQUESTS):
        exhausted = request >= max_requests or (
            deadline is not None and loop.time() >= deadline
        )
        forced = (model_turns >= max_iterations or exhausted) and not turn_open
        # tell the model why it can't call tools, so a cut-short answer says so
        last = messages[-1]
        if (
            forced
            and not told
            and last["role"] == "user"
            and isinstance(last["content"], list)
        ):
            last["content"].append({"type": "text", "text": OUT_OF_TOOLS})
            told = True
        response = await provider.client.beta.messages.create(
            model=provider.model,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            cache_control={"type": "ephemeral"},
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
            tools=tool_params,
            tool_choice=answer_now if forced else omit,
            output_config=output_config,
            container=container or omit,
        )
        log.info(
            "Agent step %d: stop=%s in=%d cached=%d out=%d",
            request,
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.cache_read_input_tokens or 0,
            response.usage.output_tokens,
        )
        messages.append({"role": "assistant", "content": response.content})
        cached = response.usage.cache_read_input_tokens or 0
        written = response.usage.cache_creation_input_tokens or 0
        usage.cached_tokens += cached
        usage.cache_write_tokens += written
        usage.input_tokens += response.usage.input_tokens + cached + written
        usage.output_tokens += response.usage.output_tokens
        if (server := response.usage.server_tool_use) is not None:
            usage.web_searches += server.web_search_requests
        usage.tool_calls += sum(
            b.type in ("tool_use", "server_tool_use") for b in response.content
        )
        if response.container is not None:
            container = response.container.id
        # responses that only carry a running script's tool calls aren't model turns
        if not all(_from_code(b) for b in response.content):
            model_turns += 1

        match response.stop_reason:
            case "tool_use":
                if forced:
                    raise AgentError(
                        "Model kept calling tools after being told to answer"
                    )
                calls = [b for b in response.content if b.type == "tool_use"]
                script_waiting = any(_from_code(b) for b in calls)
                turn_open = script_waiting or _awaits_server_tool(response.content)
                results: list[BetaContentBlockParam]
                if script_waiting and exhausted:
                    results = [_tool_result(b, STOP_SCRIPT, True) for b in calls]
                else:
                    results = list(
                        await asyncio.gather(
                            *(_run_tool(tools_by_name, b, tool_timeout) for b in calls)
                        )
                    )
                messages.append({"role": "user", "content": results})
            case "pause_turn":
                turn_open = True
            case "end_turn" | "stop_sequence":
                text = "".join(b.text for b in response.content if b.type == "text")
                return AgentResult(schema.model_validate_json(text), usage)
            case _:
                raise AgentError(f"Model stopped early ({response.stop_reason})")

    raise AgentError(f"No answer after {request + 1} requests")
