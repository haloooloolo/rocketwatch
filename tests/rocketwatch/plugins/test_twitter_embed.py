import asyncio
import json
from collections.abc import Sequence
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from rocketwatch.plugins.twitter_embed.twitter_embed import (
    REPLY_DELAY_SECONDS,
    TwitterEmbed,
    build_tweet_components,
    extract_fxtwitter_links,
    extract_tweet_links,
)
from rocketwatch.utils.config import cfg
from tests.lib.discord_harness import make_bot


@pytest.fixture
def cog() -> TwitterEmbed:
    return TwitterEmbed(make_bot())


def _tweet(**overrides: Any) -> dict[str, Any]:
    tweet: dict[str, Any] = {
        "id": "20",
        "text": "just setting up my twttr",
        "author": {
            "name": "jack",
            "screen_name": "jack",
            "avatar_url": "http://a/x.jpg",
        },
        "likes": 309793,
        "retweets": 126071,
        "replies": 17879,
        "created_timestamp": 1142974214,
    }
    tweet.update(overrides)
    return tweet


def _blob(view: discord.ui.LayoutView) -> str:
    """The serialized component payload as a searchable string."""
    return json.dumps(view.to_components(), ensure_ascii=False)


def _components_blob(
    components: Sequence[discord.ui.Item[discord.ui.LayoutView]],
) -> str:
    view = discord.ui.LayoutView(timeout=None)
    for component in components:
        view.add_item(component)
    return _blob(view)


class TestExtractTweetLinks:
    def test_matches_x_and_twitter_hosts(self) -> None:
        content = (
            "see https://x.com/jack/status/20 and "
            "https://twitter.com/nasa/status/99 and "
            "https://www.x.com/foo/status/7 and "
            "https://mobile.twitter.com/bar/status/8"
        )
        assert extract_tweet_links(content) == [
            ("jack", "20"),
            ("nasa", "99"),
            ("foo", "7"),
            ("bar", "8"),
        ]

    def test_ignores_fxtwitter_and_vxtwitter(self) -> None:
        # These already produce embeds; rewriting them would loop on our own help.
        content = (
            "https://fxtwitter.com/jack/status/20 https://vxtwitter.com/jack/status/21"
        )
        assert extract_tweet_links(content) == []

    def test_ignores_non_status_links(self) -> None:
        assert extract_tweet_links("https://x.com/jack profile only") == []

    def test_deduplicates_by_tweet_id(self) -> None:
        content = "https://x.com/jack/status/20 https://twitter.com/jack/status/20"
        assert extract_tweet_links(content) == [("jack", "20")]

    def test_returns_empty_for_no_links(self) -> None:
        assert extract_tweet_links("nothing to see here") == []


class TestExtractFxtwitterLinks:
    def test_matches_fx_family(self) -> None:
        content = (
            "https://fxtwitter.com/jack/status/20 "
            "https://fixupx.com/nasa/status/99 "
            "https://vxtwitter.com/foo/status/7"
        )
        assert extract_fxtwitter_links(content) == [
            ("jack", "20"),
            ("nasa", "99"),
            ("foo", "7"),
        ]

    def test_ignores_plain_x_and_twitter(self) -> None:
        content = "https://x.com/jack/status/20 https://twitter.com/nasa/status/99"
        assert extract_fxtwitter_links(content) == []

    def test_deduplicates_by_id(self) -> None:
        content = (
            "https://fxtwitter.com/jack/status/20 https://vxtwitter.com/jack/status/20"
        )
        assert extract_fxtwitter_links(content) == [("jack", "20")]


class TestBuildComponents:
    def test_links_to_xcancel(self) -> None:
        # The whole point: the card's link resolves to xcancel (no login gate),
        # using the real handle/id from the fetched data.
        blob = _components_blob(build_tweet_components(_tweet()))
        assert "https://xcancel.com/jack/status/20" in blob

    def test_full_card_is_a_container_then_a_separate_button_row(self) -> None:
        comps = build_tweet_components(_tweet())
        assert len(comps) == 2
        assert isinstance(comps[0], discord.ui.Container)
        assert isinstance(comps[1], discord.ui.ActionRow)
        # The button lives beneath the card, not inside it.
        assert "View on xcancel" not in _components_blob([comps[0]])

    def test_carries_text_and_author(self) -> None:
        blob = _components_blob(build_tweet_components(_tweet(text="hello world")))
        assert "hello world" in blob
        assert "jack (@jack)" in blob

    def test_single_photo_in_gallery(self) -> None:
        media = {"photos": [{"url": "http://img/1.jpg"}]}
        blob = _components_blob(build_tweet_components(_tweet(media=media)))
        assert "http://img/1.jpg" in blob

    def test_multiple_photos_all_rendered(self) -> None:
        media = {
            "photos": [
                {"url": "http://img/1.jpg"},
                {"url": "http://img/2.jpg"},
                {"url": "http://img/3.jpg"},
            ]
        }
        blob = _components_blob(build_tweet_components(_tweet(media=media)))
        assert all(f"http://img/{n}.jpg" in blob for n in (1, 2, 3))

    def test_gallery_caps_at_four_images(self) -> None:
        media = {"photos": [{"url": f"http://img/{i}.jpg"} for i in range(6)]}
        blob = _components_blob(build_tweet_components(_tweet(media=media)))
        assert "http://img/3.jpg" in blob
        assert "http://img/4.jpg" not in blob

    def test_video_in_gallery(self) -> None:
        media = {"videos": [{"url": "http://vid/clip.mp4"}]}
        blob = _components_blob(build_tweet_components(_tweet(media=media)))
        assert "http://vid/clip.mp4" in blob

    def test_quoted_tweet_included(self) -> None:
        quote = {"text": "original take", "author": {"screen_name": "satoshi"}}
        blob = _components_blob(
            build_tweet_components(_tweet(text="reply", quote=quote))
        )
        assert "reply" in blob
        assert "original take" in blob
        assert "@satoshi" in blob

    def test_footer_shows_stats_and_attribution(self) -> None:
        blob = _components_blob(build_tweet_components(_tweet(likes=5)))
        assert "via fxtwitter" in blob
        assert "5" in blob

    def test_overlong_text_truncated(self) -> None:
        blob = _components_blob(build_tweet_components(_tweet(text="x" * 5000)))
        assert ("x" * 4001) not in blob
        assert "…" in blob

    def test_native_present_posts_only_a_bare_button(self) -> None:
        # Native preview covers a plain tweet, so we add only the xcancel button
        # (no card box).
        comps = build_tweet_components(_tweet(text="covered"), native_present=True)
        assert len(comps) == 1
        assert isinstance(comps[0], discord.ui.ActionRow)
        blob = _components_blob(comps)
        assert "https://xcancel.com/jack/status/20" in blob
        assert "covered" not in blob

    def test_native_present_note_supplies_full_text(self) -> None:
        tweet = _tweet(text="the full long body", is_note_tweet=True)
        blob = _components_blob(build_tweet_components(tweet, native_present=True))
        assert "the full long body" in blob

    def test_native_present_quote_supplies_quote(self) -> None:
        quote = {"text": "original take", "author": {"screen_name": "satoshi"}}
        blob = _components_blob(
            build_tweet_components(_tweet(quote=quote), native_present=True)
        )
        assert "original take" in blob

    def test_native_present_video_supplies_player(self) -> None:
        media = {"videos": [{"url": "http://vid/clip.mp4"}]}
        blob = _components_blob(
            build_tweet_components(_tweet(media=media), native_present=True)
        )
        assert "http://vid/clip.mp4" in blob

    def test_native_playing_video_omits_player(self) -> None:
        media = {"videos": [{"url": "http://vid/clip.mp4"}]}
        blob = _components_blob(
            build_tweet_components(
                _tweet(media=media), native_present=True, native_plays_video=True
            )
        )
        assert "http://vid/clip.mp4" not in blob


def _make_message(
    content: str,
    *,
    is_bot: bool = False,
    embeds: list[Any] | None = None,
    guild_id: int | None = None,
) -> MagicMock:
    message = MagicMock()
    message.id = 123
    message.content = content
    message.author.bot = is_bot
    # Default to the Rocket Pool server, the only guild we act in.
    message.guild.id = (
        cfg.rocketpool.support.server_id if guild_id is None else guild_id
    )
    # `embeds` is the message's own (Discord-generated) preview state, read after
    # the re-fetch to decide what to contribute.
    message.embeds = embeds or []
    message.reply = AsyncMock()
    # The cog re-fetches the message after the delay; return the same mock so
    # reply assertions stay on one object.
    message.channel.fetch_message = AsyncMock(return_value=message)
    return message


def _reply_blob(message: MagicMock) -> str:
    _args, kwargs = message.reply.call_args
    return _blob(kwargs["view"])


def _reply_top_level_types(message: MagicMock) -> list[int]:
    _args, kwargs = message.reply.call_args
    view: discord.ui.LayoutView = kwargs["view"]
    return [component["type"] for component in view.to_components()]


_CONTAINER = discord.ComponentType.container.value
_ACTION_ROW = discord.ComponentType.action_row.value


class TestOnMessage:
    @pytest.fixture(autouse=True)
    def fast_sleep(self, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        # Don't actually wait the reply delay during tests.
        sleep = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep)
        return sleep

    async def test_ignores_bot_authors(self, cog: TwitterEmbed) -> None:
        cog._fetch_tweet = AsyncMock()  # type: ignore[method-assign]
        message = _make_message("https://x.com/jack/status/20", is_bot=True)
        await cog.on_message(message)

        cog._fetch_tweet.assert_not_awaited()
        message.reply.assert_not_awaited()

    async def test_no_links_does_nothing(self, cog: TwitterEmbed) -> None:
        cog._fetch_tweet = AsyncMock()  # type: ignore[method-assign]
        message = _make_message("just a normal message")
        await cog.on_message(message)

        cog._fetch_tweet.assert_not_awaited()
        message.reply.assert_not_awaited()

    async def test_ignores_other_guilds(self, cog: TwitterEmbed) -> None:
        cog._fetch_tweet = AsyncMock()  # type: ignore[method-assign]
        other = cfg.rocketpool.support.server_id + 1
        message = _make_message("https://x.com/jack/status/20", guild_id=other)
        await cog.on_message(message)

        cog._fetch_tweet.assert_not_awaited()
        message.reply.assert_not_awaited()

    async def test_ignores_dms(self, cog: TwitterEmbed) -> None:
        cog._fetch_tweet = AsyncMock()  # type: ignore[method-assign]
        message = _make_message("https://x.com/jack/status/20")
        message.guild = None
        await cog.on_message(message)

        cog._fetch_tweet.assert_not_awaited()
        message.reply.assert_not_awaited()

    async def test_replies_with_a_view_without_pinging(self, cog: TwitterEmbed) -> None:
        cog._fetch_tweet = AsyncMock(return_value=_tweet())  # type: ignore[method-assign]
        message = _make_message("look https://x.com/jack/status/20")
        await cog.on_message(message)

        message.reply.assert_awaited_once()
        _args, kwargs = message.reply.call_args
        assert kwargs["mention_author"] is False
        assert isinstance(kwargs["view"], discord.ui.LayoutView)
        assert "https://xcancel.com/jack/status/20" in _reply_blob(message)

    async def test_native_preview_contributes_only_the_link(
        self, cog: TwitterEmbed
    ) -> None:
        cog._fetch_tweet = AsyncMock(return_value=_tweet(text="covered"))  # type: ignore[method-assign]
        message = _make_message(
            "https://x.com/jack/status/20", embeds=[discord.Embed()]
        )
        await cog.on_message(message)

        blob = _reply_blob(message)
        assert "https://xcancel.com/jack/status/20" in blob
        assert "covered" not in blob

    async def test_multi_image_tweet_renders_gallery(self, cog: TwitterEmbed) -> None:
        media = {"photos": [{"url": "http://img/1.jpg"}, {"url": "http://img/2.jpg"}]}
        cog._fetch_tweet = AsyncMock(return_value=_tweet(media=media))  # type: ignore[method-assign]
        message = _make_message("https://x.com/jack/status/20")
        await cog.on_message(message)

        blob = _reply_blob(message)
        assert "http://img/1.jpg" in blob
        assert "http://img/2.jpg" in blob

    async def test_fxtwitter_link_replies_with_only_a_button(
        self, cog: TwitterEmbed
    ) -> None:
        # fxtwitter already embeds well, so we add just the xcancel button and
        # never call the API.
        cog._fetch_tweet = AsyncMock()  # type: ignore[method-assign]
        message = _make_message("https://fxtwitter.com/jack/status/20")
        await cog.on_message(message)

        cog._fetch_tweet.assert_not_awaited()
        message.reply.assert_awaited_once()
        assert "https://xcancel.com/jack/status/20" in _reply_blob(message)
        assert _reply_top_level_types(message) == [_ACTION_ROW]

    async def test_x_and_fx_links_both_handled(self, cog: TwitterEmbed) -> None:
        cog._fetch_tweet = AsyncMock(return_value=_tweet())  # type: ignore[method-assign]
        message = _make_message(
            "https://x.com/jack/status/20 https://fxtwitter.com/nasa/status/99"
        )
        await cog.on_message(message)

        blob = _reply_blob(message)
        assert "https://xcancel.com/jack/status/20" in blob  # card for the x link
        assert "https://xcancel.com/nasa/status/99" in blob  # button for the fx link

    async def test_fx_link_skipped_when_same_tweet_is_carded(
        self, cog: TwitterEmbed
    ) -> None:
        # Same tweet posted as both x and fx links: render the card, not a
        # duplicate button.
        cog._fetch_tweet = AsyncMock(return_value=_tweet())  # type: ignore[method-assign]
        message = _make_message(
            "https://x.com/jack/status/20 https://fxtwitter.com/jack/status/20"
        )
        await cog.on_message(message)

        assert _reply_top_level_types(message) == [_CONTAINER, _ACTION_ROW]

    async def test_waits_before_replying(
        self, cog: TwitterEmbed, fast_sleep: AsyncMock
    ) -> None:
        cog._fetch_tweet = AsyncMock(return_value=_tweet())  # type: ignore[method-assign]
        message = _make_message("https://x.com/jack/status/20")
        await cog.on_message(message)

        fast_sleep.assert_awaited_once_with(REPLY_DELAY_SECONDS)

    async def test_does_not_wait_when_nothing_to_post(
        self, cog: TwitterEmbed, fast_sleep: AsyncMock
    ) -> None:
        cog._fetch_tweet = AsyncMock(return_value=None)  # type: ignore[method-assign]
        message = _make_message("https://x.com/jack/status/20")
        await cog.on_message(message)

        fast_sleep.assert_not_awaited()

    async def test_unresolvable_tweet_yields_no_reply(self, cog: TwitterEmbed) -> None:
        cog._fetch_tweet = AsyncMock(return_value=None)  # type: ignore[method-assign]
        message = _make_message("https://x.com/jack/status/20")
        await cog.on_message(message)

        message.reply.assert_not_awaited()

    async def test_fetch_error_is_swallowed(self, cog: TwitterEmbed) -> None:
        cog._fetch_tweet = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        message = _make_message("https://x.com/jack/status/20")
        await cog.on_message(message)  # must not raise

        message.reply.assert_not_awaited()

    async def test_message_removed_during_delay_skips_reply(
        self, cog: TwitterEmbed
    ) -> None:
        cog._fetch_tweet = AsyncMock(return_value=_tweet())  # type: ignore[method-assign]
        message = _make_message("https://x.com/jack/status/20")
        message.channel.fetch_message = AsyncMock(
            side_effect=discord.NotFound(MagicMock(), "gone")
        )
        await cog.on_message(message)  # must not raise

        message.reply.assert_not_awaited()

    async def test_reply_http_error_is_swallowed(self, cog: TwitterEmbed) -> None:
        cog._fetch_tweet = AsyncMock(return_value=_tweet())  # type: ignore[method-assign]
        message = _make_message("https://x.com/jack/status/20")
        message.reply.side_effect = discord.HTTPException(MagicMock(), "nope")
        await cog.on_message(message)  # must not raise
