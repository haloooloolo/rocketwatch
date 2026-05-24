import asyncio
import logging
import re
from typing import Any, NamedTuple

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

# The fxtwitter family — these already render rich embeds, so we only add the
# xcancel button rather than rebuilding a card.
FX_TWEET_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:fxtwitter|fixupx|vxtwitter)\.com"
    r"/(?P<user>\w+)/status/(?P<id>\d+)",
    re.IGNORECASE,
)

MAX_TWEETS_PER_MESSAGE = 4
MAX_MESSAGE_TEXT = 4000
MAX_GALLERY_IMAGES = 4  # Discord shows at most four images in one media gallery

# Wait before replying so that (a) a message removed for spam shortly after
# posting is gone before we react, and (b) Discord has had time to attach its
# own link preview, which we check to decide what (if anything) to contribute.
REPLY_DELAY_SECONDS = 5


class _Card(NamedTuple):
    tweet: dict[str, Any]
    tweet_id: str


def _extract_status_links(
    content: str, pattern: re.Pattern[str]
) -> list[tuple[str, str]]:
    """``(user, tweet_id)`` for each match, de-duplicated by id, in order."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for match in pattern.finditer(content):
        tweet_id = match.group("id")
        if tweet_id in seen:
            continue
        seen.add(tweet_id)
        out.append((match.group("user"), tweet_id))
    return out


def extract_tweet_links(content: str) -> list[tuple[str, str]]:
    """x.com / twitter.com status links."""
    return _extract_status_links(content, TWEET_URL_RE)


def extract_fxtwitter_links(content: str) -> list[tuple[str, str]]:
    """fxtwitter / fixupx / vxtwitter status links (already embed well)."""
    return _extract_status_links(content, FX_TWEET_URL_RE)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _image_urls(media: Any) -> list[str]:
    """The tweet's photo URLs (up to four), shown as real images in a gallery."""
    if not isinstance(media, dict):
        return []
    photos = media.get("photos")
    if not isinstance(photos, list):
        return []
    urls: list[str] = []
    for photo in photos:
        if isinstance(photo, dict) and (url := photo.get("url")):
            urls.append(str(url))
        if len(urls) == MAX_GALLERY_IMAGES:
            break
    return urls


def _video_url(media: Any) -> str | None:
    """The tweet's direct video URL (raw mp4), for a media gallery item."""
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


def _native_misses_content(tweet: dict[str, Any]) -> bool:
    """Content a native X preview drops: the body of a long 'note' tweet (it gets
    truncated) and any quoted tweet (previews never include the quote).
    """
    return bool(tweet.get("is_note_tweet")) or isinstance(tweet.get("quote"), dict)


def _native_plays_video(embeds: list[discord.Embed], tweet_id: str) -> bool:
    """Whether a native preview for this tweet already carries a playable video
    (as opposed to just a still image)."""
    needle = f"/status/{tweet_id}"
    return any(e.url and needle in e.url and e.video and e.video.url for e in embeds)


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


def _screen_name_and_id(tweet: dict[str, Any]) -> tuple[str, str]:
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    assert isinstance(author, dict)
    return author.get("screen_name") or "", str(tweet.get("id") or "")


def _xcancel_url(screen_name: str, tweet_id: str) -> str:
    return f"{XCANCEL_BASE}/{screen_name}/status/{tweet_id}"


def xcancel_status_url(tweet: dict[str, Any]) -> str:
    """The freely-viewable xcancel (Nitter) URL for a tweet, from its real handle."""
    return _xcancel_url(*_screen_name_and_id(tweet))


def _body_text(tweet: dict[str, Any], limit: int = MAX_MESSAGE_TEXT) -> str:
    text = tweet.get("text") or ""
    quote = tweet.get("quote")
    if isinstance(quote, dict):
        text += _format_quote(quote)
    return _truncate(text, limit)


def _media_gallery(media: Any) -> discord.ui.MediaGallery[discord.ui.LayoutView] | None:
    """A media gallery of the tweet's photos, or its video (X disallows mixing)."""
    urls = _image_urls(media)
    if not urls and (video := _video_url(media)):
        urls = [video]
    if not urls:
        return None
    gallery: discord.ui.MediaGallery[discord.ui.LayoutView] = discord.ui.MediaGallery()
    for url in urls:
        gallery.add_item(media=url)
    return gallery


def _xcancel_button(status_url: str) -> discord.ui.ActionRow[discord.ui.LayoutView]:
    row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
    row.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.link, url=status_url, label="View on xcancel"
        )
    )
    return row


_TopLevel = (
    discord.ui.Container[discord.ui.LayoutView]
    | discord.ui.ActionRow[discord.ui.LayoutView]
)


def build_tweet_components(
    tweet: dict[str, Any],
    *,
    native_present: bool = False,
    native_plays_video: bool = False,
) -> list[_TopLevel]:
    """Build the Components-V2 reply pieces for one tweet.

    With no native preview the card is full (author, text, media, stats). With a
    native preview we add a card only for what it drops — long "note" text,
    quoted tweets, and a video it renders as a still image. The xcancel link
    button is always a separate row *beneath* the card (and the only piece when
    there is no card to add).
    """
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    assert isinstance(author, dict)
    status_url = xcancel_status_url(tweet)
    media = tweet.get("media")

    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        accent_colour=TWITTER_COLOR
    )
    has_content = False

    if not native_present:
        # No avatar: a V2 Section thumbnail can't be resized and makes the header
        # too tall. Author is plain bold text, not a masked link — masked links
        # render literally when the name contains emoji (e.g. flags); the xcancel
        # button below is the link.
        author_line = f"**{_author_label(author)}**"
        footer_line = f"-# {_format_footer(tweet)}"
        container.add_item(discord.ui.TextDisplay(author_line))
        # Total text per V2 message is capped; budget the body around the rest.
        body_limit = MAX_MESSAGE_TEXT - len(author_line) - len(footer_line)
        if body := _body_text(tweet, body_limit):
            container.add_item(discord.ui.TextDisplay(body))
        if (gallery := _media_gallery(media)) is not None:
            container.add_item(gallery)
        container.add_item(discord.ui.TextDisplay(footer_line))
        has_content = True
    else:
        if _native_misses_content(tweet) and (body := _body_text(tweet)):
            container.add_item(discord.ui.TextDisplay(body))
            has_content = True
        if (
            _video_url(media)
            and not native_plays_video
            and (gallery := _media_gallery(media)) is not None
        ):
            container.add_item(gallery)
            has_content = True

    components: list[_TopLevel] = [container] if has_content else []
    components.append(_xcancel_button(status_url))
    return components


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

        tweet_links = extract_tweet_links(message.content)
        fx_links = extract_fxtwitter_links(message.content)
        if not tweet_links and not fx_links:
            return

        cards: list[_Card] = []
        for user, tweet_id in tweet_links[:MAX_TWEETS_PER_MESSAGE]:
            try:
                tweet = await self._fetch_tweet(user, tweet_id)
            except Exception:
                log.warning("Failed to fetch tweet %s", tweet_id, exc_info=True)
                continue
            if tweet is not None:
                cards.append(_Card(tweet=tweet, tweet_id=tweet_id))

        # fxtwitter-family links already render rich embeds, so we only offer the
        # xcancel button (no API fetch) — skipping any tweet we're already carding.
        card_ids = {card.tweet_id for card in cards}
        fx_buttons = [
            (user, tweet_id)
            for user, tweet_id in fx_links[:MAX_TWEETS_PER_MESSAGE]
            if tweet_id not in card_ids
        ]

        if not cards and not fx_buttons:
            return

        await asyncio.sleep(REPLY_DELAY_SECONDS)
        try:
            message = await message.channel.fetch_message(message.id)
        except discord.HTTPException:
            # Original is gone (e.g. removed as spam) during the delay.
            return

        native = message.embeds
        view = discord.ui.LayoutView(timeout=None)
        for card in cards:
            for component in build_tweet_components(
                card.tweet,
                native_present=bool(native),
                native_plays_video=_native_plays_video(native, card.tweet_id),
            ):
                view.add_item(component)
        for user, tweet_id in fx_buttons:
            view.add_item(_xcancel_button(_xcancel_url(user, tweet_id)))

        try:
            await message.reply(view=view, mention_author=False)
        except discord.NotFound:
            # Original was deleted between the re-fetch and our reply — expected.
            log.info("Skipped xcancel reply; original message is gone")
        except discord.HTTPException as e:
            # Anything else (e.g. a malformed payload) is a real bug; surface it.
            await self.bot.report_error(e)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(TwitterEmbed(bot))
