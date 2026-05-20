import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from rocketwatch.plugins.twitter_embed.twitter_embed import (
    REPLY_DELAY_SECONDS,
    TwitterEmbed,
    build_tweet_embed,
    extract_tweet_links,
)
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


class TestBuildTweetEmbed:
    def test_every_link_points_at_xcancel(self) -> None:
        # The whole point of the module: the clickable card resolves to xcancel
        # (no login gate), using the real handle/id from the fetched data.
        embed = build_tweet_embed(_tweet())
        assert embed.author.url == "https://xcancel.com/jack/status/20"

    def test_description_carries_the_tweet_text(self) -> None:
        embed = build_tweet_embed(_tweet(text="hello world"))
        assert embed.description == "hello world"

    def test_author_label_shows_name_and_handle(self) -> None:
        embed = build_tweet_embed(_tweet())
        assert embed.author.name == "jack (@jack)"

    def test_single_photo_becomes_the_image(self) -> None:
        media = {"photos": [{"url": "http://img/1.jpg"}]}
        embed = build_tweet_embed(_tweet(media=media))
        assert embed.image.url == "http://img/1.jpg"

    def test_multi_photo_prefers_the_mosaic(self) -> None:
        media = {
            "photos": [{"url": "http://img/1.jpg"}, {"url": "http://img/2.jpg"}],
            "mosaic": {"formats": {"jpeg": "http://img/mosaic.jpg"}},
        }
        embed = build_tweet_embed(_tweet(media=media))
        assert embed.image.url == "http://img/mosaic.jpg"

    def test_video_uses_thumbnail_and_links_to_the_clip(self) -> None:
        media = {
            "videos": [
                {"url": "http://vid/clip.mp4", "thumbnail_url": "http://vid/t.jpg"}
            ]
        }
        embed = build_tweet_embed(_tweet(media=media))
        assert embed.image.url == "http://vid/t.jpg"
        assert any(
            field.value and "http://vid/clip.mp4" in field.value
            for field in embed.fields
        )

    def test_quoted_tweet_is_included(self) -> None:
        quote = {
            "text": "original take",
            "author": {"name": "Sat", "screen_name": "satoshi"},
        }
        embed = build_tweet_embed(_tweet(text="my reply", quote=quote))
        assert embed.description is not None
        assert "my reply" in embed.description
        assert "original take" in embed.description
        assert "@satoshi" in embed.description

    def test_footer_shows_stats_and_attribution(self) -> None:
        embed = build_tweet_embed(_tweet(likes=5, retweets=2, replies=1))
        assert embed.footer.text is not None
        assert "via fxtwitter" in embed.footer.text
        assert "5" in embed.footer.text

    def test_overlong_text_is_truncated(self) -> None:
        embed = build_tweet_embed(_tweet(text="x" * 5000))
        assert embed.description is not None
        assert len(embed.description) <= 4000


def _make_message(
    content: str,
    *,
    is_bot: bool = False,
    embeds: list[Any] | None = None,
) -> MagicMock:
    message = MagicMock()
    message.id = 123
    message.content = content
    message.author.bot = is_bot
    # `embeds` is the message's own (Discord-generated) preview state, read after
    # the re-fetch to decide whether our embed is needed.
    message.embeds = embeds or []
    message.reply = AsyncMock()
    # The cog re-fetches the message after the delay; return the same mock so
    # reply/embeds assertions stay on one object.
    message.channel.fetch_message = AsyncMock(return_value=message)
    return message


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

    async def test_replies_with_embed_without_pinging(self, cog: TwitterEmbed) -> None:
        cog._fetch_tweet = AsyncMock(return_value=_tweet())  # type: ignore[method-assign]
        message = _make_message("look https://x.com/jack/status/20")
        await cog.on_message(message)

        message.reply.assert_awaited_once()
        _args, kwargs = message.reply.call_args
        assert kwargs["mention_author"] is False
        assert len(kwargs["embeds"]) == 1
        assert kwargs["embeds"][0].author.url == "https://xcancel.com/jack/status/20"
        # The xcancel link is also message text, bracketed so Discord won't add
        # its own preview alongside our embed.
        assert kwargs["content"] == "<https://xcancel.com/jack/status/20>"

    async def test_omits_embed_when_message_already_has_preview(
        self, cog: TwitterEmbed
    ) -> None:
        # The original already shows a preview and the (text-only) tweet has no
        # image, so our embed would be redundant — post just the link.
        cog._fetch_tweet = AsyncMock(return_value=_tweet())  # type: ignore[method-assign]
        message = _make_message(
            "https://x.com/jack/status/20", embeds=[discord.Embed()]
        )
        await cog.on_message(message)

        message.reply.assert_awaited_once()
        _args, kwargs = message.reply.call_args
        assert kwargs["embeds"] == []
        assert kwargs["content"] == "<https://xcancel.com/jack/status/20>"

    async def test_omits_embed_for_single_image_with_preview(
        self, cog: TwitterEmbed
    ) -> None:
        # A native X preview already shows a single image, so ours adds nothing.
        media = {"photos": [{"url": "http://img/1.jpg"}]}
        cog._fetch_tweet = AsyncMock(return_value=_tweet(media=media))  # type: ignore[method-assign]
        message = _make_message(
            "https://x.com/jack/status/20", embeds=[discord.Embed()]
        )
        await cog.on_message(message)

        message.reply.assert_awaited_once()
        _args, kwargs = message.reply.call_args
        assert kwargs["embeds"] == []

    async def test_keeps_embed_for_multi_image_tweet_despite_preview(
        self, cog: TwitterEmbed
    ) -> None:
        # A native preview shows at most one image; ours surfaces the rest.
        media = {
            "photos": [{"url": "http://img/1.jpg"}, {"url": "http://img/2.jpg"}],
            "mosaic": {"formats": {"jpeg": "http://img/mosaic.jpg"}},
        }
        cog._fetch_tweet = AsyncMock(return_value=_tweet(media=media))  # type: ignore[method-assign]
        message = _make_message(
            "https://x.com/jack/status/20", embeds=[discord.Embed()]
        )
        await cog.on_message(message)

        message.reply.assert_awaited_once()
        _args, kwargs = message.reply.call_args
        assert len(kwargs["embeds"]) == 1

    async def test_keeps_embed_for_video_tweet_despite_preview(
        self, cog: TwitterEmbed
    ) -> None:
        # A native preview can't play video, so ours (thumbnail + link) is kept.
        media = {"videos": [{"url": "http://vid/clip.mp4"}]}
        cog._fetch_tweet = AsyncMock(return_value=_tweet(media=media))  # type: ignore[method-assign]
        message = _make_message(
            "https://x.com/jack/status/20", embeds=[discord.Embed()]
        )
        await cog.on_message(message)

        message.reply.assert_awaited_once()
        _args, kwargs = message.reply.call_args
        assert len(kwargs["embeds"]) == 1

    async def test_waits_before_replying(
        self, cog: TwitterEmbed, fast_sleep: AsyncMock
    ) -> None:
        # Give spam removal a window to take the message (and our reply) down.
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
        # Deleted/private tweets resolve to None and must not produce an empty reply.
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
        # If the message is gone when we re-fetch (e.g. removed as spam), we bail.
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
