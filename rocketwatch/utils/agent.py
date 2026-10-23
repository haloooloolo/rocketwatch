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


@dataclass(frozen=True)
class AgentResult[R: BaseModel]:
    output: R
    tool_calls: int
    # input_tokens includes cache reads and writes
    input_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0


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


def estimate_cost(result: AgentResult[Any], model: str) -> float | None:
    """Approximate USD cost of a run, or None for a model without known prices."""
    if (price := PRICES.get(model)) is None:
        return None
    uncached = result.input_tokens - result.cached_tokens - result.cache_write_tokens
    tokens = (
        uncached * price.input
        + result.cached_tokens * price.cache_read
        + result.cache_write_tokens * price.cache_write
        + result.output_tokens * price.output
    )
    return tokens / 1_000_000 + result.web_searches * WEB_SEARCH_PRICE


class AgentError(Exception):
    pass


def _build_messages(turns: list[Turn]) -> list[BetaMessageParam]:
    return [
        {
            "role": turn.role,
            "content": AnthropicProvider._build_user_content(turn.text, turn.images),
        }
        for turn in turns
    ]


CODE_EXECUTION = "code_execution_20260120"
# tool calls a running script makes still cost a request each, so they get a
# separate, larger budget than the model's own turns
MAX_REQUESTS = 200


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


async def _run_tool(
    tools: dict[str, AgentTool], block: BetaToolUseBlock
) -> BetaToolResultBlockParam:
    def result(content: str, is_error: bool = False) -> BetaToolResultBlockParam:
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": content,
            "is_error": is_error,
        }

    if (tool := tools.get(block.name)) is None:
        return result(f"Unknown tool {block.name!r}", is_error=True)
    # each call runs in its own task, so this doesn't leak between calls
    _called_from_code.set(_from_code(block))
    try:
        args = tool.input_model.model_validate(block.input)
    except ValidationError as err:
        return result(f"Invalid input: {err}", is_error=True)
    try:
        return result(await tool.run(args))
    except ToolError as err:
        return result(str(err), is_error=True)
    except Exception as err:
        log.warning("Tool %s failed", block.name, exc_info=True)
        return result(f"{type(err).__name__}: {err}", is_error=True)


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
    effort: Effort = "high",
    max_tokens: int = 16000,
) -> AgentResult[T]:
    """Let the model call tools until it produces an answer matching schema.

    server_tools are tools Anthropic runs itself, such as web search. With
    code_execution, the model can also run Python in Anthropic's sandbox and
    call the tools from there.
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
    messages = _build_messages(turns)
    tool_calls = 0
    usage = {"input": 0, "cached": 0, "written": 0, "output": 0, "searches": 0}
    model_turns = 0
    container: str | None = None
    script_waiting = False

    for request in range(MAX_REQUESTS):
        forced = model_turns >= max_iterations and not script_waiting
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
        usage["cached"] += cached
        usage["written"] += written
        usage["input"] += response.usage.input_tokens + cached + written
        usage["output"] += response.usage.output_tokens
        if (server := response.usage.server_tool_use) is not None:
            usage["searches"] += server.web_search_requests
        tool_calls += sum(
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
                results: list[BetaContentBlockParam] = list(
                    await asyncio.gather(*(_run_tool(tools_by_name, b) for b in calls))
                )
                messages.append({"role": "user", "content": results})
            case "pause_turn":
                continue
            case "end_turn" | "stop_sequence":
                text = "".join(b.text for b in response.content if b.type == "text")
                return AgentResult(
                    schema.model_validate_json(text),
                    tool_calls,
                    input_tokens=usage["input"],
                    cached_tokens=usage["cached"],
                    cache_write_tokens=usage["written"],
                    output_tokens=usage["output"],
                    web_searches=usage["searches"],
                )
            case _:
                raise AgentError(f"Model stopped early ({response.stop_reason})")

    raise AgentError(f"No answer after {request + 1} requests")
