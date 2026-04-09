from collections.abc import Sequence
from urllib import parse

import regex as re
from anyascii import anyascii
from cachetools import TTLCache
from discord import Emoji, Member, Message, PartialEmoji, User

type _Keyword = str | Sequence["_Keyword"]


class ScamChecks:
    def __init__(self) -> None:
        self._message_react_cache: TTLCache[
            int, dict[PartialEmoji | Emoji | str, set[User | Member]]
        ] = TTLCache(maxsize=1000, ttl=300)
        self.markdown_link_pattern = re.compile(
            r"(?<=\[)([^/\] ]*).+?(?<=\(https?:\/\/)([^/\)]*)"
        )
        self.basic_url_pattern = re.compile(
            r"https?:\/\/?([/\\@\-_0-9a-zA-Z]+\.)+[\\@\-_0-9a-zA-Z]+"
        )
        self.invite_pattern = re.compile(
            r"((discord(app)?\.com\/(invite|oauth2))|((dsc|dcd|discord)\.gg))(\\|\/)(?P<code>[a-zA-Z0-9]+)"
        )
        # Detects URLs broken across lines (with optional blockquote "> " prefixes) to evade filters
        _brk = r"(?:[\s>\u2060\u200b\ufeff]*\n[\s>\u2060\u200b\ufeff]*)"  # newline with optional blockquote/zero-width chars
        _ws = r"[\s>]*"
        self.obfuscated_url_pattern = re.compile(
            rf"<{_ws}ht{_brk}tp|"  # <ht\n> tp
            rf"<{_ws}ma{_ws}i{_brk}l{_ws}t{_ws}o|"  # <ma\n> i\n> L\n> To (mailto)
            rf"<d{_brk}?i{_brk}?s{_brk}?c{_brk}?o{_brk}?r|"  # <discord: broken across lines
            rf"di{_brk}sco{_brk}rd(?!\.(?:com|gg|py|js|net|org))|"  # di\nsco\nrd (not discord.com etc)
            rf"dis{_brk}cord",  # dis\ncord (alternate break position)
            re.IGNORECASE,
        )
        # Detects fullwidth/homoglyph dots used to disguise domains
        self.homoglyph_url_pattern = re.compile(
            r"https?://[^\s]*[\uff61\u3002\uff0e]",  # fullwidth/CJK dots
        )
        # Extracts username from X/Twitter URL variants
        _x_domains = r"(?:x|twitter|fxtwitter|fixvx|xcancel|vxtwitter)\.com"
        self.x_url_pattern = re.compile(
            rf"https?://(?:www\.)?{_x_domains}/(\w+)", re.IGNORECASE
        )

    def run_all(self, message: Message) -> str | None:
        checks = [
            self._obfuscated_url,
            self._ticket_system,
            self._suspicious_x_account,
            self._suspicious_link,
            self._discord_invite,
            self._tap_on_this,
            self._spam_wall,
        ]
        for check in checks:
            if reason := check(message):
                return reason
        return None

    @staticmethod
    def _get_message_content(message: Message) -> str:
        text = ""
        if message.content:
            content = message.content
            content = content.replace("\n> ", "")
            content = content.replace("\n", "")
            text += content + "\n"
        if message.embeds:
            for embed in message.embeds:
                text += f"---\n Embed: {embed.title}\n{embed.description}\n---\n"

        text = parse.unquote(text)
        text = anyascii(text)
        text = text.lower()
        return text

    def _discord_invite(self, message: Message) -> str | None:
        # Only check message content, not embeds (legit videos/links have discord invites in embeds)
        if not message.content:
            return None
        content = message.content
        content = parse.unquote(content)
        content = anyascii(content)
        content = content.lower()
        if match := self.invite_pattern.search(content):
            link = match.group(0)
            trusted_domains = [
                "youtu.be",
                "youtube.com",
                "tenor.com",
                "giphy.com",
                "imgur.com",
                "bluesky.app",
            ]
            if not any(domain in link for domain in trusted_domains):
                return "Invite to external server"
        return None

    def _tap_on_this(self, message: Message) -> str | None:
        txt = self._get_message_content(message)
        keywords = [("tap on", "click on"), "proper"]
        return "Tap on deez nuts nerd" if self.__txt_contains(txt, keywords) else None

    def _obfuscated_url(self, message: Message) -> str | None:
        if not message.content:
            return None

        default_reason = "URL obfuscation"
        # Line-broken protocol/scheme
        if self.obfuscated_url_pattern.search(message.content):
            return default_reason
        # Fullwidth/homoglyph dots in domain
        if self.homoglyph_url_pattern.search(message.content):
            return default_reason
        # Heavily percent-encoded ASCII in URL (encoding ASCII is suspicious; non-ASCII like Cyrillic is normal)
        if re.search(r"https?://[^\s]*(?:%[0-7][0-9a-fA-F]){5}", message.content):
            return default_reason
        # Markdown link where visible text looks like a different domain than the actual URL
        content = parse.unquote(message.content)
        content = anyascii(content).lower()
        for m in self.markdown_link_pattern.findall(content):
            if "." in m[0] and m[0].rstrip(".") != m[1].rstrip("."):
                return "Visible text changes link domain"

        return None

    def _ticket_system(self, message: Message) -> str | None:
        txt = self._get_message_content(message)
        if not self.basic_url_pattern.search(txt):
            return None

        default_reason = "There is no ticket system in this server"

        # High-confidence scam indicators (don't need URL trust check)
        strong_keywords = (
            (
                "support team",
                "supp0rt",
                "🎫",
                ":ticket:",
                "🎟️",
                ":tickets:",
                "m0d",
                "tlcket",
                "relate your issue",
            ),
            [("relay"), ("query", "question", "inquiry")],
            [("instant", "live"), "chat"],
            [("submit"), ("question", "issue", "query")],
        )
        content_only = txt.split("---")[0]
        # Auto-generated embeds from video platforms may contain event/ticket
        # language (e.g. YouTube 🎫 TICKETS) — only check content for those.
        rich_embed_domains = ("youtube.com", "youtu.be", "twitch.tv")
        content_urls = list(self.basic_url_pattern.finditer(content_only))
        if content_urls and all(
            any(d in m.group(0) for d in rich_embed_domains) for m in content_urls
        ):
            strong_check_text = re.sub(r"https?://\S+", "", content_only)
        else:
            strong_check_text = re.sub(r"https?://\S+", "", txt)
        if self.__txt_contains(strong_check_text, strong_keywords):
            return default_reason

        # Short directive messages with a URL ("ask here", "get help here")
        content_only = txt.split("---")[0].strip()  # exclude embeds
        if len(content_only) < 120 and self.basic_url_pattern.search(txt):
            directives = ("ask here", "get help", "help here", "click here", "go here")
            if any(d in content_only for d in directives):
                return default_reason

        # Weaker keywords: only check short messages (long technical discussions cause false positives)
        content_txt = self._get_message_content(message)
        content_only_txt = content_txt.split("---")[0]  # strip embed text
        if len(content_only_txt) > 500:
            return None

        ticket_keywords = [
            ("support", "open", "create", "raise", "raisse"),
            "ticket",
        ]
        # For short messages, also check full text (including embeds) for ticket keywords.
        # Scammers use embeds (via X posts, Discord invites) to carry ticket/support language.
        # Only use the ticket pattern here; the contact+admin pattern is too broad for embed text
        # (e.g. "administration" in news articles matches "admin").
        if len(content_only_txt) <= 200 and self.__txt_contains(txt, ticket_keywords):
            return default_reason

        trusted_url_domains = (
            "youtu.be",
            "youtube.com",
            "twitter.com",
            "x.com",
            "fxtwitter.com",
            "fixvx.com",
            "fxbsky.app",
            "reddit.com",
            "github.com",
            "etherscan.io",
            "beaconcha.in",
            "rocketpool.net",
            "docs.rocketpool.net",
            "rocketpool.support",
            "xcancel.com",
            "steely-test.org",
            "rocketdash.net",
            "validatorqueue.com",
            "checkpointz",
            "discord.com",
            "forms.gle",
            "google.com",
        )
        content_urls = list(self.basic_url_pattern.finditer(content_only_txt))
        if not content_urls or all(
            any(domain in m.group(0) for domain in trusted_url_domains)
            for m in content_urls
        ):
            return None

        weak_keywords = (
            [("support", "open", "create", "raise", "raisse"), "ticket"],
            [
                (
                    "contact",
                    "reach out",
                    "report",
                    [("talk", "speak"), ("to", "with")],
                    "ask",
                ),
                ("admin", "mod", "administrator", "moderator", "team"),
            ],
        )
        if self.__txt_contains(content_only_txt, weak_keywords):
            return default_reason

        return None

    @staticmethod
    def __txt_contains(txt: str, kw: _Keyword) -> bool:
        match kw:
            case str():
                return kw in txt
            case tuple():
                return any(map(lambda w: ScamChecks.__txt_contains(txt, w), kw))
            case list():
                return all(map(lambda w: ScamChecks.__txt_contains(txt, w), kw))
        return False

    def _suspicious_link(self, message: Message) -> str | None:
        txt = self._get_message_content(message)
        if "http" not in txt:
            return None
        hosting_domains = ("pages.dev", "web.app", "vercel.app")
        if any(d in txt for d in hosting_domains) and re.search(
            r"\b(?:mint|opensea|airdrop|claim|reward|free)\b", txt
        ):
            return "The linked website is most likely a wallet drainer"
        return None

    def _suspicious_x_account(self, message: Message) -> str | None:
        if not message.content:
            return None
        suspicious_keywords = ("support", "ticket", "helpdesk", "assist")
        for m in self.x_url_pattern.finditer(message.content):
            username = m.group(1).lower()
            if any(kw in username for kw in suspicious_keywords):
                return "Link to suspicious X account"
        return None

    def _spam_wall(self, message: Message) -> str | None:
        if not message.content or len(message.content) < 100:
            return None
        content = message.content
        # Spoiler wall: many spoiler tags with minimal visible content
        if content.count("||") >= 20:
            stripped = re.sub(r"\|\||[\s\u200b_]|https?://\S+", "", content).strip()
            if len(stripped) < 10:
                return "Spoiler wall spam"
        # Invisible character wall: mostly blank/invisible characters
        visible = re.sub(
            r"[\s\u2800\u200b\u200c\u200d\u2060\ufeff\U000e0000-\U000e007f]",
            "",
            content,
        )
        if len(visible) < 10 and len(content) > 200:
            return "Invisible character spam"
        return None
