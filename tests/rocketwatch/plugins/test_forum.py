from typing import Any
from unittest.mock import AsyncMock

import pytest

from rocketwatch.plugins.forum import forum as forum_module
from rocketwatch.plugins.forum.forum import Forum
from tests.lib.discord_harness import make_bot, make_interaction


class _FakeResp:
    def __init__(self, json_data: Any) -> None:
        self._json = json_data

    async def json(self) -> Any:
        return self._json


class _FakeSession:
    def __init__(self, json_data: Any) -> None:
        self._json = json_data

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def get(self, *_a: Any, **_k: Any) -> _FakeResp:
        return _FakeResp(self._json)


def _patch_http(monkeypatch: pytest.MonkeyPatch, json_data: Any) -> None:
    monkeypatch.setattr(
        forum_module.aiohttp, "ClientSession", lambda *a, **k: _FakeSession(json_data)
    )


def _topic_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": 1,
        "fancy_title": "A Topic",
        "slug": "a-topic",
        "posts_count": 12,
        "created_at": "2024-01-01T00:00:00Z",
        "last_posted_at": "2024-02-01T12:00:00Z",
        "views": 345,
        "like_count": 6,
    }
    base.update(overrides)
    return base


class TestTopicModel:
    def test_url_and_str(self) -> None:
        topic = Forum.Topic(
            id=7,
            title="Hello",
            slug="hello",
            post_count=1,
            created_at=0,
            last_post_at=0,
            views=0,
            like_count=0,
        )
        assert topic.url == "https://dao.rocketpool.net/t/hello/7"
        assert str(topic) == "Hello"


class TestUserModel:
    def test_str_prefers_name_then_username(self) -> None:
        named = Forum.User(1, "alice123", "Alice", 0, 0, 0)
        anon = Forum.User(2, "bob123", None, 0, 0, 0)
        assert str(named) == "Alice"
        assert str(anon) == "bob123"
        assert anon.url == "https://dao.rocketpool.net/u/bob123"


class TestParseTopics:
    def test_converts_iso_timestamps_to_epoch(self) -> None:
        topics = Forum._parse_topics([_topic_dict()])
        assert len(topics) == 1
        t = topics[0]
        assert t.title == "A Topic"
        # 2024-01-01T00:00:00Z → known epoch.
        assert t.created_at == 1_704_067_200
        assert t.last_post_at > t.created_at


class TestGetPopularTopics:
    async def test_parses_top_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_http(
            monkeypatch,
            {"topic_list": {"topics": [_topic_dict(id=1), _topic_dict(id=2)]}},
        )
        topics = await Forum.get_popular_topics("monthly")
        assert [t.id for t in topics] == [1, 2]


class TestGetRecentTopics:
    async def test_parses_latest_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_http(monkeypatch, {"topic_list": {"topics": [_topic_dict(id=9)]}})
        topics = await Forum.get_recent_topics()
        assert topics[0].id == 9


class TestGetTopUsers:
    async def test_parses_directory_items(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_http(
            monkeypatch,
            {
                "directory_items": [
                    {
                        "id": 1,
                        "user": {"username": "alice", "name": "Alice"},
                        "topic_count": 3,
                        "post_count": 10,
                        "likes_received": 25,
                    },
                    {
                        "id": 2,
                        "user": {"username": "bob", "name": ""},
                        "topic_count": 1,
                        "post_count": 2,
                        "likes_received": 4,
                    },
                ]
            },
        )
        users = await Forum.get_top_users("monthly", "likes_received")
        assert users[0].name == "Alice"
        # Empty name normalises to None.
        assert users[1].name is None


class TestTopForumPostsCommand:
    async def test_renders_topics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            Forum,
            "get_popular_topics",
            AsyncMock(
                return_value=Forum._parse_topics(
                    [_topic_dict(id=1, fancy_title="First")]
                )
            ),
        )
        cog = Forum(make_bot())
        interaction = make_interaction()
        await cog.top_forum_posts.callback(cog, interaction, period="weekly")
        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.title == "Top Forum Posts (weekly)"
        assert "First" in embed.description

    async def test_no_topics_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Forum, "get_popular_topics", AsyncMock(return_value=[]))
        cog = Forum(make_bot())
        interaction = make_interaction()
        await cog.top_forum_posts.callback(cog, interaction)
        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.description == "No topics found."


class TestTopForumUsersCommand:
    async def test_renders_users(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            Forum,
            "get_top_users",
            AsyncMock(return_value=[Forum.User(1, "alice", "Alice", 3, 10, 25)]),
        )
        cog = Forum(make_bot())
        interaction = make_interaction()
        await cog.top_forum_users.callback(cog, interaction)
        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert "Alice" in embed.description

    async def test_no_users_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Forum, "get_top_users", AsyncMock(return_value=[]))
        cog = Forum(make_bot())
        interaction = make_interaction()
        await cog.top_forum_users.callback(cog, interaction)
        embed = interaction.followup.send.call_args.kwargs["embed"]
        assert embed.description == "No users found."
