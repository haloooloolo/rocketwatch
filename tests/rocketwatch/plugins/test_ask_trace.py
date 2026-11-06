from datetime import UTC, datetime
from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.ask.trace import MAX_CALLS, MAX_INPUT_CHARS, AnswerTraces
from rocketwatch.utils.agent import ToolCall

pytestmark = pytest.mark.integration_db

OWNER_ID = 1  # make_cfg's owner
SENT = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def traces(mongo_db: AsyncDatabase[dict[str, Any]]) -> AnswerTraces:
    return AnswerTraces(mongo_db.ask_traces)


async def test_lists_calls_and_when(traces: AnswerTraces) -> None:
    await traces.save(
        5,
        7,
        [
            ToolCall("mongo_find", {"collection": "minipools"}),
            ToolCall("web_search", {"query": "rETH"}),
        ],
    )
    note = await traces.note(5, SENT, 7)
    assert f"<t:{int(SENT.timestamp())}>" in note
    assert '- mongo_find {"collection": "minipools"}' in note
    assert '- web_search {"query": "rETH"}' in note


async def test_script_calls_are_counted(traces: AnswerTraces) -> None:
    await traces.save(
        5, 7, [ToolCall("contract_call", {"a": i}, True) for i in range(3)]
    )
    assert "- from scripts: contract_call x3" in await traces.note(5, SENT, 7)


async def test_inputs_such_as_block_numbers_are_kept(traces: AnswerTraces) -> None:
    await traces.save(5, 7, [ToolCall("contract_call", {"block": 21000000})])
    assert '{"block": 21000000}' in await traces.note(5, SENT, 7)


async def test_scripts_keep_more_of_their_code(traces: AnswerTraces) -> None:
    code = "x" * 2000 + "block = 21000000"
    await traces.save(5, 7, [ToolCall("code_execution", {"code": code})])
    assert "block = 21000000" in await traces.note(5, SENT, 7)


async def test_long_traces_are_cut(traces: AnswerTraces) -> None:
    calls = [ToolCall("eth_rpc", {"p": "x" * 2000}) for _ in range(MAX_CALLS + 4)]
    await traces.save(5, 7, calls)
    note = await traces.note(5, SENT, 7)
    assert note.count("- eth_rpc") == MAX_CALLS
    assert "- and 4 more" in note
    assert len(note) < (MAX_CALLS + 1) * (MAX_INPUT_CHARS + 20) + 300


async def test_no_tools(traces: AnswerTraces) -> None:
    await traces.save(5, 7, [])
    assert "You called no tools" in await traces.note(5, SENT, 7)


async def test_answers_without_a_record_say_so(traces: AnswerTraces) -> None:
    assert "weren't recorded" in await traces.note(5, SENT, 7)


async def test_others_see_only_the_tool_names_of_owner_runs(
    traces: AnswerTraces,
) -> None:
    await traces.save(
        5, OWNER_ID, [ToolCall("mongo_find", {"collection": "scam_reports"})]
    )
    assert "scam_reports" not in await traces.note(5, SENT, 7)
    assert "- mongo_find" in await traces.note(5, SENT, 7)
    assert "scam_reports" in await traces.note(5, SENT, OWNER_ID)
