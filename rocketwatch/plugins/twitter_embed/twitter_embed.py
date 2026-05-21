import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

import aiohttp
import discord
import humanize
from discord import Message
from discord.ext import commands

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.config import cfg

log = logging.getLogger("rocketwatch.twitter_embed")

#: fxtwitter serves Discord-friendly tweet data but its links route through to the
#: login-gated x.com; xcancel (a Nitter instance) is freely viewable but embeds
#: poorly. We borrow fxtwitter's content and point every link at xcancel.
API_BASE = "https://api.fxtwitter.com"
XCANCEL_BASE = "https://xcancel.com"

TWITTER_COLOR = discord.Color(0x1DA1F2)
API_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Only the real twitter/x hosts, with the scheme required so we don't match the
# "twitter.com" / "x.com" substrings inside fxtwitter.com, vxtwitter.com, etc.
TWEET_URL_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/(?P<user>\w+)/status/(?P<id>\d+)",
    re.IGNORECASE,
)

MAX_TWEETS_PER_MESSAGE = 4
MAX_DESCRIPTION = 4000

# Wait before replying so that (a) a message removed for spam shortly after
# posting is gone before we react, and (b) Discord has had time to attach its
# own link preview, which we check to decide whether our embed is even needed.
REPLY_DELAY_SECONDS = 5


def extract_tweet_links(content: str) -> list[tuple[str, str]]:
    """Return ``(user, tweet_id)`` for each twitter/x status link, de-duplicated
    by id and in order of first appearance.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for match in TWEET_URL_RE.finditer(content):
        tweet_id = match.group("id")
        if tweet_id in seen:
            continue
        seen.add(tweet_id)
        out.append((match.group("user"), tweet_id))
    return out


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _select_image(media: Any) -> str | None:
    """Pick the best single still image: a combined mosaic for multi-photo tweets,
    otherwise the first photo, otherwise a video's thumbnail.
    """
    if not isinstance(media, dict):
        return None

    mosaic = media.get("mosaic")
    if isinstance(mosaic, dict):
        formats = mosaic.get("formats")
        if isinstance(formats, dict) and formats.get("jpeg"):
            return str(formats["jpeg"])
        if mosaic.get("url"):
            return str(mosaic["url"])

    photos = media.get("photos")
    if (
        isinstance(photos, list)
        and photos
        and isinstance(photos[0], dict)
        and (url := photos[0].get("url"))
    ):
        return str(url)

    videos = media.get("videos")
    if (
        isinstance(videos, list)
        and videos
        and isinstance(videos[0], dict)
        and (thumbnail := videos[0].get("thumbnail_url"))
    ):
        return str(thumbnail)

    return None


def _select_video_url(media: Any) -> str | None:
    """A bot-built embed can't autoplay video, so we surface a direct link."""
    if not isinstance(media, dict):
        return None
    videos = media.get("videos")
    if (
        isinstance(videos, list)
        and videos
        and isinstance(videos[0], dict)
        and (url := videos[0].get("url"))
    ):
        return str(url)
    return None


def _exceeds_native_preview(tweet: dict[str, Any]) -> bool:
    """Whether our embed shows media a native X preview can't: more than one image
    (X previews render at most one) or a video (which it can't play inline).
    """
    media = tweet.get("media")
    photos = media.get("photos") if isinstance(media, dict) else None
    photo_count = len(photos) if isinstance(photos, list) else 0
    return photo_count > 1 or _select_video_url(media) is not None


def _author_label(author: dict[str, Any]) -> str:
    screen_name = author.get("screen_name") or ""
    name = author.get("name") or screen_name
    return f"{name} (@{screen_name})" if screen_name else str(name)


def _format_quote(quote: dict[str, Any]) -> str:
    author = quote.get("author")
    header = _author_label(author) if isinstance(author, dict) else ""
    text = quote.get("text") or ""
    body = "\n".join(f"> {line}" for line in text.splitlines())
    return f"\n\n**↘ Quoting {header}**\n{body}".rstrip()


def _format_footer(tweet: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, emoji in (("likes", "❤"), ("retweets", "🔁"), ("replies", "💬")):
        value = tweet.get(key)
        if isinstance(value, int):
            parts.append(f"{emoji} {humanize.intcomma(value)}")
    parts.append("via fxtwitter")
    return " · ".join(parts)


def xcancel_status_url(tweet: dict[str, Any]) -> str:
    """The freely-viewable xcancel (Nitter) URL for a tweet, from its real handle."""
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    assert isinstance(author, dict)
    screen_name = author.get("screen_name") or ""
    tweet_id = str(tweet.get("id") or "")
    return f"{XCANCEL_BASE}/{screen_name}/status/{tweet_id}"


def build_tweet_embed(tweet: dict[str, Any]) -> discord.Embed:
    """Build a tweet card from fxtwitter data whose every link points at xcancel."""
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    assert isinstance(author, dict)

    status_url = xcancel_status_url(tweet)

    description = tweet.get("text") or ""
    quote = tweet.get("quote")
    if isinstance(quote, dict):
        description += _format_quote(quote)

    embed = discord.Embed(
        description=_truncate(description, MAX_DESCRIPTION),
        color=TWITTER_COLOR,
    )
    embed.set_author(
        name=_truncate(_author_label(author), 256),
        url=status_url,
        icon_url=author.get("avatar_url") or None,
    )

    timestamp = tweet.get("created_timestamp")
    if isinstance(timestamp, int | float):
        embed.timestamp = datetime.fromtimestamp(timestamp, tz=UTC)

    if image_url := _select_image(tweet.get("media")):
        embed.set_image(url=image_url)

    if video_url := _select_video_url(tweet.get("media")):
        embed.add_field(name="​", value=f"[▶ Video]({video_url})", inline=False)

    embed.set_footer(text=_format_footer(tweet))
    return embed


class TwitterEmbed(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    async def _fetch_tweet(self, user: str, tweet_id: str) -> dict[str, Any] | None:
        url = f"{API_BASE}/{user}/status/{tweet_id}"
        async with (
            aiohttp.ClientSession(timeout=API_TIMEOUT) as session,
            session.get(url) as resp,
        ):
            if resp.status != 200:
                return None
            data = await resp.json()

        if not isinstance(data, dict) or data.get("code") != 200:
            return None
        tweet = data.get("tweet")
        return tweet if isinstance(tweet, dict) else None

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        if message.author.bot:
            return

        if (
            message.guild is None
            or message.guild.id != cfg.rocketpool.support.server_id
        ):
            return

        links = extract_tweet_links(message.content)
        if not links:
            return

        built: list[tuple[discord.Embed, str, bool]] = []
        for user, tweet_id in links[:MAX_TWEETS_PER_MESSAGE]:
            try:
                tweet = await self._fetch_tweet(user, tweet_id)
            except Exception:
                log.warning("Failed to fetch tweet %s", tweet_id, exc_info=True)
                continue
            if tweet is not None:
                embed = build_tweet_embed(tweet)
                url = xcancel_status_url(tweet)
                built.append((embed, url, _exceeds_native_preview(tweet)))

        if not built:
            return

        await asyncio.sleep(REPLY_DELAY_SECONDS)
        try:
            message = await message.channel.fetch_message(message.id)
        except discord.HTTPException:
            # Original is gone (e.g. removed as spam) during the delay.
            return

        # Our embed only adds value when the message has no preview of its own,
        # or when the tweet has media that preview can't fully show; otherwise
        # just post the link.
        original_has_preview = bool(message.embeds)
        embeds = [
            embed
            for embed, _url, extra_media in built
            if extra_media or not original_has_preview
        ]

        # Angle brackets keep each link clickable while suppressing Discord's own
        # (poor) xcancel preview.
        content = "\n".join(f"<{url}>" for _embed, url, _extra in built)

        try:
            await message.reply(content=content, embeds=embeds, mention_author=False)
        except discord.HTTPException:
            log.info("Skipped xcancel reply; original message is gone", exc_info=True)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(TwitterEmbed(bot))
