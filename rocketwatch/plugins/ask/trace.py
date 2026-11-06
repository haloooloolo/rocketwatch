import json
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pymongo.asynchronous.collection import AsyncCollection

from rocketwatch.utils.agent import ToolCall
from rocketwatch.utils.config import cfg

MAX_CALLS = 30
MAX_INPUT_CHARS = 500
# scripts hold the inputs of the calls they make, which aren't listed one by one
MAX_SCRIPT_CHARS = 3000


class AnswerTraces:
    """The tool calls behind each answer, by the answer's message ID, so a
    follow-up knows what was done. Results aren't kept: they'd add a lot of
    tokens, and the owner's could hold private data."""

    def __init__(self, collection: AsyncCollection[dict[str, Any]]) -> None:
        self.collection = collection

    async def save(self, answer_id: int, asker: int, calls: Sequence[ToolCall]) -> None:
        direct = [c for c in calls if not c.from_code]
        await self.collection.insert_one(
            {
                "_id": answer_id,
                "asker": asker,
                "calls": [
                    {
                        "tool": c.name,
                        "input": json.dumps(c.input, default=str)[
                            : MAX_SCRIPT_CHARS
                            if c.name == "code_execution"
                            else MAX_INPUT_CHARS
                        ],
                    }
                    for c in direct[:MAX_CALLS]
                ],
                "more": max(0, len(direct) - MAX_CALLS),
                "script_calls": Counter(c.name for c in calls if c.from_code),
            }
        )

    async def note(self, answer_id: int, sent: datetime, reader: int) -> str:
        """What reader's follow-up is told about an earlier answer."""
        intro = (
            "[Note from the bot, not from a person: the answer above is yours, "
            f"sent at <t:{int(sent.timestamp())}>, and its figures are from then. "
        )
        doc = await self.collection.find_one({"_id": answer_id})
        if doc is None:
            return intro + "The tool calls behind it weren't recorded.]"
        script_calls: dict[str, int] = doc["script_calls"]
        if not doc["calls"] and not script_calls:
            return intro + "You called no tools for it.]"
        # the owner's inputs can name private collections and what was searched
        private = doc["asker"] == cfg.discord.owner.user_id
        private = private and reader != cfg.discord.owner.user_id
        lines = [
            f"- {c['tool']}" if private else f"- {c['tool']} {c['input']}"
            for c in doc["calls"]
        ]
        if doc["more"]:
            lines.append(f"- and {doc['more']} more")
        if script_calls:
            counts = ", ".join(f"{n} x{count}" for n, count in script_calls.items())
            lines.append(f"- from scripts: {counts}")
        listed = "\n".join(lines)
        return f"{intro}Tools you called for it, whose results you no longer have:\n{listed}]"
