import pickle
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import TextChannel
from discord.ext import tasks
from eth_typing import BlockNumber
from PIL import Image as PillowImage
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.plugins.event_core.event_core import EventCore
from rocketwatch.utils import shared_w3
from rocketwatch.utils.config import StatusMessageConfig, cfg
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.event import Event, EventPlugin
from rocketwatch.utils.image import Image
from rocketwatch.utils.status import StatusPlugin
from tests.lib.discord_harness import make_bot

DEFAULT, DAO = 100, 200


class ScriptedPlugin(EventPlugin):
    def __init__(self, bot: Any, events: list[Event]) -> None:
        super().__init__(bot)
        self.events = events
        self.new_events: list[Event] = []
        self.ranges: list[tuple[int, int]] = []

    async def get_past_events(
        self, from_block: BlockNumber, to_block: BlockNumber
    ) -> list[Event]:
        self.ranges.append((from_block, to_block))
        return [e for e in self.events if from_block <= e.block_number <= to_block]

    async def _get_new_events(self) -> list[Event]:
        return self.new_events


class ScriptedStatus(StatusPlugin):
    async def get_status(self) -> Embed:
        return Embed(title="All good")


def _event(name: str, uid: str, block: int = 1, **kwargs: Any) -> Event:
    return Event(
        embed=Embed(title=uid),
        topic="events",
        event_name=name,
        unique_id=uid,
        block_number=BlockNumber(block),
        **kwargs,
    )


class Channels:
    """Discord channels by id, recording what was sent."""

    def __init__(self) -> None:
        self.by_id: dict[int, MagicMock] = {}
        self._next_message_id = 1000

    def get(self, channel_id: int) -> MagicMock:
        if channel_id not in self.by_id:
            channel = MagicMock(spec=TextChannel)
            channel.send = AsyncMock(side_effect=self._message)
            channel.fetch_message = AsyncMock(side_effect=self._fetched)
            self.by_id[channel_id] = channel
        return self.by_id[channel_id]

    def _message(self, *_: Any, **__: Any) -> Any:
        self._next_message_id += 1
        return MagicMock(id=self._next_message_id)

    @staticmethod
    def _fetched(message_id: int) -> Any:
        return MagicMock(id=message_id, delete=AsyncMock(), edit=AsyncMock())

    def sent_titles(self, channel_id: int) -> list[str]:
        return [
            c.kwargs["embed"].title for c in self.get(channel_id).send.call_args_list
        ]


@pytest.fixture
def chain_head(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    get_block_number = AsyncMock(return_value=5)
    eth = MagicMock(get_block_number=get_block_number)
    monkeypatch.setattr(shared_w3.w3, "_instance", MagicMock(eth=eth))
    return get_block_number


@pytest.fixture
def channels() -> Channels:
    return Channels()


@pytest.fixture
def core(
    monkeypatch: pytest.MonkeyPatch,
    mongo_db: AsyncDatabase[dict[str, Any]],
    channels: Channels,
) -> EventCore:
    monkeypatch.setattr(tasks.Loop, "start", lambda *_, **__: None)
    bot = make_bot(db=mongo_db)
    bot.cogs = {}
    bot.get_or_fetch_channel = AsyncMock(side_effect=channels.get)
    core = EventCore(bot)
    core.channels = {"default": DEFAULT, "dao": DAO}
    return core


def _bot(core: EventCore) -> MagicMock:
    bot: MagicMock = core.bot  # type: ignore[assignment]
    return bot


def _plugin(core: EventCore, events: list[Event]) -> ScriptedPlugin:
    plugin = ScriptedPlugin(core.bot, events)
    _bot(core).cogs["plugin"] = plugin
    return plugin


class TestGatherNewEvents:
    async def test_queues_events_routed_by_name_prefix(
        self, core: EventCore, chain_head: MagicMock
    ) -> None:
        _plugin(core, [_event("dao_vote", "a"), _event("rpl_stake", "b")])

        await core.gather_new_events()

        queued = {
            d["_id"]: d["channel_id"]
            async for d in core.bot.db.event_queue.find({"message_id": None})
        }
        assert queued == {"a": DAO, "b": DEFAULT}

    async def test_already_queued_event_is_not_queued_again(
        self, core: EventCore, chain_head: MagicMock
    ) -> None:
        await core.bot.db.event_queue.insert_one(
            {"_id": "a", "message_id": 1, "block_number": 0}
        )
        _plugin(core, [_event("rpl_stake", "a"), _event("rpl_stake", "b")])

        await core.gather_new_events()

        assert await core.bot.db.event_queue.count_documents({}) == 2
        assert (await core.bot.db.event_queue.find_one({"_id": "a"}) or {})[
            "message_id"
        ] == 1

    async def test_duplicate_id_in_one_batch_does_not_block_the_rest(
        self, core: EventCore, chain_head: MagicMock
    ) -> None:
        _plugin(
            core,
            [
                _event("rpl_stake", "a"),
                _event("rpl_stake", "a"),
                _event("rpl_stake", "b"),
            ],
        )

        await core.gather_new_events()

        ids = await core.bot.db.event_queue.distinct("_id")
        assert sorted(ids) == ["a", "b"]
        checked = await core.bot.db.last_checked_block.find_one({"_id": "events"})
        assert checked is not None
        assert checked["block"] == 5

    async def test_catches_up_in_batches_then_follows_head(
        self, core: EventCore, chain_head: MagicMock
    ) -> None:
        chain_head.return_value = 25
        plugin = _plugin(core, [])
        plugin.new_events = [_event("rpl_stake", "live", block=26)]

        for _ in range(3):
            await core.gather_new_events()

        assert plugin.ranges == [(1, 10), (11, 20), (21, 25)]
        assert core.at_head
        # live tracking picks up right after the last scanned block
        assert plugin.last_served_block == 25

        await core.gather_new_events()

        assert await core.bot.db.event_queue.find_one({"_id": "live"}) is not None

    async def test_resumes_after_last_checked_block(
        self, core: EventCore, chain_head: MagicMock
    ) -> None:
        chain_head.return_value = 55
        await core.bot.db.last_checked_block.insert_one({"_id": "events", "block": 50})
        plugin = _plugin(core, [])

        await core.gather_new_events()

        assert plugin.ranges == [(51, 55)]


async def _queue(core: EventCore, *docs: dict[str, Any]) -> None:
    await core.bot.db.event_queue.insert_many(
        [
            {
                "event_name": "rpl_stake",
                "channel_id": DEFAULT,
                "message_id": None,
                "image": None,
                "thumbnail": None,
                **doc,
            }
            for doc in docs
        ]
    )


class TestProcessEventQueue:
    async def test_posts_pending_events_in_order_once(
        self, core: EventCore, channels: Channels
    ) -> None:
        await _queue(
            core,
            {"_id": "late", "score": 2, "embed": pickle.dumps(Embed(title="late"))},
            {"_id": "early", "score": 1, "embed": pickle.dumps(Embed(title="early"))},
            {
                "_id": "posted",
                "score": 0,
                "embed": pickle.dumps(Embed(title="posted")),
                "message_id": 7,
            },
        )

        await core.process_event_queue()
        await core.process_event_queue()

        assert channels.sent_titles(DEFAULT) == ["early", "late"]
        assert await core.bot.db.event_queue.count_documents({"message_id": None}) == 0

    async def test_attaches_image_and_thumbnail(
        self, core: EventCore, channels: Channels
    ) -> None:
        image = pickle.dumps(Image(PillowImage.new("RGB", (2, 2))))
        await _queue(
            core,
            {
                "_id": "a",
                "score": 1,
                "embed": pickle.dumps(Embed(title="a")),
                "image": image,
                "thumbnail": image,
            },
        )

        await core.process_event_queue()

        kwargs = channels.get(DEFAULT).send.call_args.kwargs
        assert [f.filename for f in kwargs["files"]] == [
            "rpl_stake_img.png",
            "rpl_stake_thumb.png",
        ]
        assert kwargs["embed"].image.url == "attachment://rpl_stake_img.png"

    async def test_unreadable_event_is_reported_and_skipped(
        self, core: EventCore, channels: Channels
    ) -> None:
        await _queue(
            core,
            {"_id": "bad", "score": 1, "embed": b"not a pickle"},
            {"_id": "good", "score": 2, "embed": pickle.dumps(Embed(title="good"))},
        )

        await core.process_event_queue()

        assert channels.sent_titles(DEFAULT) == ["good"]
        _bot(core).report_error.assert_awaited()

    async def test_status_message_is_removed_before_posting(
        self, core: EventCore, channels: Channels
    ) -> None:
        # so the status message is re-posted below the new events
        await core.bot.db.state_messages.insert_one(
            {"_id": "default", "channel_id": DEFAULT, "message_id": 5}
        )
        await _queue(
            core, {"_id": "a", "score": 1, "embed": pickle.dumps(Embed(title="a"))}
        )

        await core.process_event_queue()

        channels.get(DEFAULT).fetch_message.assert_awaited_once_with(5)
        assert await core.bot.db.state_messages.count_documents({}) == 0

    async def test_manually_deleted_status_message_does_not_block_posting(
        self, core: EventCore, channels: Channels
    ) -> None:
        await core.bot.db.state_messages.insert_one(
            {"_id": "default", "channel_id": DEFAULT, "message_id": 5}
        )
        channels.get(DEFAULT).fetch_message.side_effect = discord.NotFound(
            MagicMock(status=404), "Unknown Message"
        )
        await _queue(
            core, {"_id": "a", "score": 1, "embed": pickle.dumps(Embed(title="a"))}
        )

        await core.process_event_queue()

        assert channels.sent_titles(DEFAULT) == ["a"]
        assert await core.bot.db.state_messages.count_documents({}) == 0


@pytest.fixture
def status_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, StatusMessageConfig]:
    assert cfg._instance is not None
    status = {"default": StatusMessageConfig(plugin="Status", cooldown=60)}
    events = cfg._instance.events.model_copy(update={"status_message": status})
    monkeypatch.setattr(
        cfg, "_instance", cfg._instance.model_copy(update={"events": events})
    )
    return status


async def _status_doc(core: EventCore) -> dict[str, Any]:
    doc = await core.bot.db.state_messages.find_one({"_id": "default"})
    assert doc is not None
    return doc


class TestStatusMessages:
    @pytest.fixture(autouse=True)
    def _status_plugin(self, core: EventCore) -> None:
        _bot(core).cogs["Status"] = ScriptedStatus(core.bot)

    async def test_posts_status_when_none_exists(
        self, core: EventCore, channels: Channels, status_config: Any
    ) -> None:
        await core.update_status_messages()

        assert channels.sent_titles(DEFAULT) == ["All good"]
        assert channels.get(DEFAULT).send.call_args.kwargs["silent"] is True
        assert (await _status_doc(core))["state"] == "OK"

    async def test_recent_status_is_left_alone(
        self, core: EventCore, channels: Channels, status_config: Any
    ) -> None:
        await core.update_status_messages()
        channels.get(DEFAULT).send.reset_mock()

        await core.update_status_messages()

        channels.get(DEFAULT).send.assert_not_awaited()
        channels.get(DEFAULT).fetch_message.assert_not_awaited()

    async def test_stale_status_is_edited_in_place(
        self, core: EventCore, channels: Channels, status_config: Any
    ) -> None:
        await core.update_status_messages()
        await core.bot.db.state_messages.update_one(
            {"_id": "default"},
            {"$set": {"sent_at": datetime.now(UTC) - timedelta(minutes=5)}},
        )
        channels.get(DEFAULT).send.reset_mock()
        edited = MagicMock(edit=AsyncMock())
        channels.get(DEFAULT).fetch_message = AsyncMock(return_value=edited)

        await core.update_status_messages()

        edited.edit.assert_awaited_once()
        channels.get(DEFAULT).send.assert_not_awaited()

    async def test_status_deleted_by_hand_is_reposted(
        self, core: EventCore, channels: Channels, status_config: Any
    ) -> None:
        await core.update_status_messages()
        await core.bot.db.state_messages.update_one(
            {"_id": "default"},
            {"$set": {"sent_at": datetime.now(UTC) - timedelta(minutes=5)}},
        )
        channels.get(DEFAULT).fetch_message = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "Unknown Message")
        )

        await core.update_status_messages()

        assert channels.sent_titles(DEFAULT) == ["All good", "All good"]
        assert await core.bot.db.state_messages.count_documents({}) == 1

    async def test_announcement_replaces_status(
        self, core: EventCore, channels: Channels, status_config: Any
    ) -> None:
        await core.bot.db.support_bot.insert_one(
            {"_id": "announcement", "title": "Maintenance", "description": "soon"}
        )

        await core.update_status_messages()

        assert channels.sent_titles(DEFAULT) == ["Maintenance"]

    async def test_shows_catch_up_progress_when_far_behind(
        self, core: EventCore, channels: Channels, status_config: Any
    ) -> None:
        core._catchup_start_block = BlockNumber(0)
        core.head_block = BlockNumber(50)
        core.latest_block = BlockNumber(200)

        await core.update_status_messages()

        embed = channels.get(DEFAULT).send.call_args.kwargs["embed"]
        assert embed.title == "⏳ Catching Up"
        fields = {f.name: f.value for f in embed.fields}
        assert fields["Progress"].endswith("25.0%")
        assert fields["Blocks Remaining"] == "150"

    async def test_status_without_config_is_removed(
        self, core: EventCore, channels: Channels
    ) -> None:
        await core.bot.db.state_messages.insert_one(
            {
                "_id": "old",
                "channel_id": DEFAULT,
                "message_id": 5,
                "sent_at": datetime.now(UTC),
                "state": "OK",
            }
        )

        await core.update_status_messages()

        assert await core.bot.db.state_messages.count_documents({}) == 0


class TestErrorState:
    async def test_error_is_reported_and_shown_in_status_channels(
        self, core: EventCore, channels: Channels, status_config: Any
    ) -> None:
        error = RuntimeError("rpc down")

        await core.on_error(error)

        _bot(core).report_error.assert_awaited_once_with(error)
        assert core.state == EventCore.State.ERROR
        assert channels.sent_titles(DEFAULT) == [
            ":warning: Failure in Event Processing"
        ]
        assert (await _status_doc(core))["state"] == "ERROR"

    async def test_repeated_errors_do_not_repost_the_interrupt(
        self, core: EventCore, channels: Channels, status_config: Any
    ) -> None:
        await core.on_error(RuntimeError("rpc down"))
        await core.on_error(RuntimeError("rpc still down"))

        assert len(channels.sent_titles(DEFAULT)) == 1

    async def test_recovers_after_success(self, core: EventCore) -> None:
        await core.on_error(RuntimeError("rpc down"))

        await core.on_success()

        assert core.state == EventCore.State.OK
