from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from discord.abc import Messageable
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.pinned_messages.pinned_messages import PinnedMessages
from tests.lib.discord_harness import make_bot, make_interaction


def _make_cog(bot: Any) -> PinnedMessages:
    # __init__ may start the tasks.loop; force is_ready False to skip it.
    bot.is_ready = MagicMock(return_value=False)
    return PinnedMessages(bot)


async def _run_loop(cog: PinnedMessages) -> None:
    # Invoke the underlying coroutine, not the Loop scheduler.
    await cog.run_loop.coro(cog)


def _history(messages: list[Any]) -> Any:
    async def _gen(*_a: Any, **_k: Any) -> AsyncIterator[Any]:
        for m in messages:
            yield m

    return _gen


def _channel(*, history_msgs: list[Any] | None = None) -> MagicMock:
    ch = MagicMock(spec=Messageable)
    ch.send = AsyncMock()
    ch.fetch_message = AsyncMock()
    if history_msgs is not None:
        ch.history = _history(history_msgs)
    return ch


class TestPinCommand:
    async def test_channel_not_found(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        bot = make_bot(db=mongo_db)
        bot.get_channel = MagicMock(return_value=None)
        cog = _make_cog(bot)
        interaction = make_interaction()
        await cog.pin.callback(
            cog, interaction, channel_id=1, title="t", description="d"
        )
        assert interaction.followup.send.call_args.args[0] == "Channel not found"

    async def test_creates_new_pin(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        bot = make_bot(db=mongo_db)
        channel = MagicMock()
        channel.id = 555
        bot.get_channel = MagicMock(return_value=channel)
        cog = _make_cog(bot)
        interaction = make_interaction()
        await cog.pin.callback(
            cog, interaction, channel_id=555, title="Hello", description="World"
        )
        doc = await mongo_db.pinned_messages.find_one({"channel_id": 555})
        assert doc is not None
        assert doc["title"] == "Hello"
        assert doc["disabled"] is False
        assert "Created" in interaction.followup.send.call_args.args[0]

    async def test_updates_existing_pin(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.pinned_messages.insert_one(
            {
                "channel_id": 555,
                "message_id": 1,
                "title": "old",
                "content": "old",
                "disabled": True,
                "cleaned_up": True,
                "created_at": datetime.now(UTC),
            }
        )
        bot = make_bot(db=mongo_db)
        channel = MagicMock()
        channel.id = 555
        bot.get_channel = MagicMock(return_value=channel)
        cog = _make_cog(bot)
        interaction = make_interaction()
        await cog.pin.callback(
            cog, interaction, channel_id=555, title="new", description="newer"
        )
        doc = await mongo_db.pinned_messages.find_one({"channel_id": 555})
        assert doc is not None
        assert doc["title"] == "new"
        # Reset for the loop to re-send.
        assert doc["disabled"] is False
        assert doc["message_id"] is None
        assert "Updated" in interaction.followup.send.call_args.args[0]


class TestUnpinCommand:
    async def test_channel_not_found(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        bot = make_bot(db=mongo_db)
        bot.get_channel = MagicMock(return_value=None)
        cog = _make_cog(bot)
        interaction = make_interaction()
        await cog.unpin.callback(cog, interaction, channel_id="1")
        assert interaction.followup.send.call_args.args[0] == "Channel not found"

    async def test_no_pinned_message(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        bot = make_bot(db=mongo_db)
        channel = MagicMock()
        channel.id = 555
        bot.get_channel = MagicMock(return_value=channel)
        cog = _make_cog(bot)
        interaction = make_interaction()
        await cog.unpin.callback(cog, interaction, channel_id="555")
        assert interaction.followup.send.call_args.args[0] == "No pinned message found"

    async def test_already_disabled(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.pinned_messages.insert_one({"channel_id": 555, "disabled": True})
        bot = make_bot(db=mongo_db)
        channel = MagicMock()
        channel.id = 555
        bot.get_channel = MagicMock(return_value=channel)
        cog = _make_cog(bot)
        interaction = make_interaction()
        await cog.unpin.callback(cog, interaction, channel_id="555")
        assert "already disabled" in interaction.followup.send.call_args.args[0]

    async def test_soft_deletes(self, mongo_db: AsyncDatabase[dict[str, Any]]) -> None:
        await mongo_db.pinned_messages.insert_one(
            {"channel_id": 555, "disabled": False}
        )
        bot = make_bot(db=mongo_db)
        channel = MagicMock()
        channel.id = 555
        bot.get_channel = MagicMock(return_value=channel)
        cog = _make_cog(bot)
        interaction = make_interaction()
        await cog.unpin.callback(cog, interaction, channel_id="555")
        doc = await mongo_db.pinned_messages.find_one({"channel_id": 555})
        assert doc is not None
        assert doc["disabled"] is True
        assert "Disabled" in interaction.followup.send.call_args.args[0]


class TestRunLoop:
    async def test_marks_old_message_disabled(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        # Older than 6h, still enabled, already cleaned_up so no channel work.
        await mongo_db.pinned_messages.insert_one(
            {
                "channel_id": 1,
                "message_id": 2,
                "disabled": False,
                "cleaned_up": True,
                "created_at": datetime.now(UTC) - timedelta(hours=7),
            }
        )
        bot = make_bot(db=mongo_db)
        cog = _make_cog(bot)
        await _run_loop(cog)
        doc = await mongo_db.pinned_messages.find_one({"channel_id": 1})
        assert doc is not None
        assert doc["disabled"] is True

    async def test_cleans_up_disabled_message(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.pinned_messages.insert_one(
            {
                "channel_id": 1,
                "message_id": 2,
                "disabled": True,
                "cleaned_up": False,
                "created_at": datetime.now(UTC),
            }
        )
        msg = MagicMock()
        msg.delete = AsyncMock()
        channel = _channel()
        channel.fetch_message = AsyncMock(return_value=msg)
        bot = make_bot(db=mongo_db)
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        cog = _make_cog(bot)
        await _run_loop(cog)
        msg.delete.assert_awaited_once()
        doc = await mongo_db.pinned_messages.find_one({"channel_id": 1})
        assert doc is not None
        assert doc["cleaned_up"] is True

    async def test_resends_active_message_when_not_recent(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.pinned_messages.insert_one(
            {
                "channel_id": 1,
                "message_id": 2,
                "title": "Pinned",
                "content": "Body",
                "disabled": False,
                "cleaned_up": False,
                "created_at": datetime.now(UTC),
            }
        )
        old_msg = MagicMock()
        old_msg.delete = AsyncMock()
        new_msg = MagicMock()
        new_msg.id = 999
        # history doesn't contain message_id 2 → must resend.
        other = MagicMock()
        other.id = 111
        channel = _channel(history_msgs=[other])
        channel.fetch_message = AsyncMock(return_value=old_msg)
        channel.send = AsyncMock(return_value=new_msg)
        bot = make_bot(db=mongo_db)
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        cog = _make_cog(bot)
        await _run_loop(cog)
        old_msg.delete.assert_awaited_once()
        channel.send.assert_awaited_once()
        doc = await mongo_db.pinned_messages.find_one({"channel_id": 1})
        assert doc is not None
        assert doc["message_id"] == 999

    async def test_skips_resend_when_message_recent(
        self, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.pinned_messages.insert_one(
            {
                "channel_id": 1,
                "message_id": 2,
                "title": "Pinned",
                "content": "Body",
                "disabled": False,
                "cleaned_up": False,
                "created_at": datetime.now(UTC),
            }
        )
        recent = MagicMock()
        recent.id = 2  # the pinned message is among the latest → no resend
        channel = _channel(history_msgs=[recent])
        channel.send = AsyncMock()
        bot = make_bot(db=mongo_db)
        bot.get_or_fetch_channel = AsyncMock(return_value=channel)
        cog = _make_cog(bot)
        await _run_loop(cog)
        channel.send.assert_not_awaited()
