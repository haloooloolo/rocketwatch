import asyncio
import contextlib
import io
import logging
from datetime import UTC, datetime, timedelta
from urllib import parse

import regex as re
from anyascii import anyascii
from cachetools import TTLCache
from discord import (
    AppCommandType,
    ButtonStyle,
    Color,
    DeletedReferencedMessage,
    File,
    Guild,
    Interaction,
    Member,
    Message,
    RawBulkMessageDeleteEvent,
    RawMessageDeleteEvent,
    RawThreadDeleteEvent,
    RawThreadUpdateEvent,
    Reaction,
    Thread,
    User,
    errors,
    ui,
)
from discord.app_commands import ContextMenu, command, guilds
from discord.ext.commands import Cog

from rocketwatch import RocketWatch
from utils.config import cfg
from utils.embeds import Embed

log = logging.getLogger("rocketwatch.scam_detection")


class ScamDetection(Cog):
    class Color:
        ALERT = Color.from_rgb(255, 0, 0)
        WARN = Color.from_rgb(255, 165, 0)
        OK = Color.from_rgb(0, 255, 0)

    @staticmethod
    def is_reputable(user: Member) -> bool:
        return any(
            (
                user.id == cfg.discord.owner.user_id,
                user.id in cfg.rocketpool.support.user_ids,
                {role.id for role in user.roles} & set(cfg.rocketpool.support.role_ids),
                user.guild_permissions.moderate_members,
            )
        )

    class RemovalVoteView(ui.View):
        THRESHOLD = 5

        def __init__(self, plugin: "ScamDetection", reportable: Message | Thread):
            super().__init__(timeout=None)
            self.plugin = plugin
            self.reportable = reportable
            self.safu_votes = set()

        @ui.button(label="Mark Safu", style=ButtonStyle.blurple)
        async def mark_safe(self, interaction: Interaction, button: ui.Button) -> None:
            log.info(
                f"User {interaction.user.id} marked message {interaction.message.id} as safe"
            )

            reportable_repr = type(self.reportable).__name__.lower()
            if interaction.user.id in self.safu_votes:
                log.debug(
                    f"User {interaction.user.id} already voted on {reportable_repr}"
                )
                await interaction.response.send_message(
                    content="You already voted!", ephemeral=True
                )
                return

            if interaction.user.is_timed_out():
                log.debug(
                    f"Timed-out user {interaction.user.id} tried to vote on {self.reportable}"
                )
                return None

            if isinstance(self.reportable, Message):
                reported_user = self.reportable.author
                db_filter = {"type": "message", "message_id": self.reportable.id}
                required_lock = self.plugin._message_report_lock
            elif isinstance(self.reportable, Thread):
                reported_user = self.reportable.owner
                db_filter = {"type": "thread", "channel_id": self.reportable.id}
                required_lock = self.plugin._thread_report_lock
            else:
                log.warning(f"Unknown reportable type {type(self.reportable)}")
                return None

            if interaction.user == reported_user:
                log.debug(
                    f"User {interaction.user.id} tried to mark their own {reportable_repr} as safe"
                )
                await interaction.response.send_message(
                    content=f"You can't vote on your own {reportable_repr}!",
                    ephemeral=True,
                )
                return

            self.safu_votes.add(interaction.user.id)

            if ScamDetection.is_reputable(interaction.user):
                user_repr = interaction.user.mention
            elif len(self.safu_votes) >= self.THRESHOLD:
                user_repr = "the community"
            else:
                button.label = f"Mark Safu ({len(self.safu_votes)}/{self.THRESHOLD})"
                await interaction.response.edit_message(view=self)
                return

            await interaction.message.delete()

            async with required_lock:
                report = await self.plugin.bot.db.scam_reports.find_one(db_filter)
                await self.plugin._update_report(
                    report, f"This has been marked as safe by {user_repr}."
                )
                await self.plugin.bot.db.scam_reports.update_one(
                    db_filter, {"$set": {"warning_id": None}}
                )
                await interaction.response.send_message(
                    content="Warning removed!", ephemeral=True
                )

    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self._message_report_lock = asyncio.Lock()
        self._thread_report_lock = asyncio.Lock()
        self._user_report_lock = asyncio.Lock()
        self._message_react_cache = TTLCache(maxsize=1000, ttl=300)
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
            rf"di{_brk}sco{_brk}rd(?!\.(?:com|gg|py|js|net|org))",  # di\nsco\nrd (not discord.com etc)
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

        self.message_report_menu = ContextMenu(
            name="Report Message",
            callback=self.manual_message_report,
            guild_ids=[cfg.rocketpool.support.server_id],
        )
        self.bot.tree.add_command(self.message_report_menu)
        self.user_report_menu = ContextMenu(
            name="Report User",
            callback=self.manual_user_report,
            type=AppCommandType.user,
            guild_ids=[cfg.rocketpool.support.server_id],
        )
        self.bot.tree.add_command(self.user_report_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(
            self.message_report_menu.name, type=self.message_report_menu.type
        )
        self.bot.tree.remove_command(
            self.user_report_menu.name, type=self.user_report_menu.type
        )

    @staticmethod
    def _get_message_content(
        message: Message, *, preserve_formatting: bool = False
    ) -> str:
        text = ""
        if message.content:
            content = message.content
            if not preserve_formatting:
                content = content.replace("\n> ", "")
                content = content.replace("\n", "")
            text += content + "\n"
        if message.embeds:
            for embed in message.embeds:
                text += f"---\n Embed: {embed.title}\n{embed.description}\n---\n"

        if not preserve_formatting:
            text = parse.unquote(text)
            text = anyascii(text)
            text = text.lower()

        return text

    async def _generate_message_report(
        self, message: Message, reason: str
    ) -> tuple[Embed, Embed, File] | None:
        try:
            message = await message.channel.fetch_message(message.id)
            if isinstance(message, DeletedReferencedMessage):
                return None
        except errors.NotFound:
            return None

        if await self.bot.db.scam_reports.find_one(
            {"type": "message", "message_id": message.id}
        ):
            log.info(f"Found existing report for message {message.id} in database")
            return None

        warning = Embed(title="🚨 Possible Scam Detected")
        warning.color = self.Color.ALERT
        warning.description = f"**Reason**: {reason}\n"

        report = warning.copy()
        warning.set_footer(
            text="This message will be deleted once the suspicious message is removed."
        )

        report.description += (
            "\n"
            f"User ID:    `{message.author.id}` ({message.author.mention})\n"
            f"Message ID: `{message.id}` ({message.jump_url})\n"
            f"Channel ID: `{message.channel.id}` ({message.channel.jump_url})\n"
            "\n"
            "Original message has been attached as a file.\n"
            "Please review and take appropriate action."
        )

        text = self._get_message_content(message, preserve_formatting=True)
        with io.StringIO(text) as f:
            attachment = File(f, filename="original_message.txt")

        return warning, report, attachment

    async def _generate_thread_report(
        self, thread: Thread, reason: str
    ) -> tuple[Embed, Embed] | None:
        try:
            thread = await thread.guild.fetch_channel(thread.id)
        except (errors.NotFound, errors.Forbidden):
            return None

        if await self.bot.db.scam_reports.find_one(
            {"type": "thread", "channel_id": thread.id}
        ):
            log.info(f"Found existing report for thread {thread.id} in database")
            return None

        warning = Embed(title="🚨 Possible Scam Detected")
        warning.color = self.Color.ALERT
        warning.description = f"**Reason**: {reason}\n"

        report = warning.copy()
        warning.set_footer(
            text=(
                "There is no ticket system for support on this server.\n"
                "Ignore this thread and any invites or DMs you may receive."
            )
        )
        thread_owner = await self.bot.get_or_fetch_user(thread.owner_id)
        report.description += (
            "\n"
            f"Thread Name: `{thread.name}`\n"
            f"User ID:     `{thread_owner.id}` ({thread_owner.mention})\n"
            f"Thread ID:   `{thread.id}` ({thread.jump_url})\n"
            "\n"
            "Please review and take appropriate action."
        )
        return warning, report

    async def _add_message_report_to_db(
        self,
        message: Message,
        reason: str,
        warning_msg: Message | None,
        report_msg: Message,
    ) -> None:
        await self.bot.db.scam_reports.insert_one(
            {
                "type": "message",
                "guild_id": message.guild.id,
                "channel_id": message.channel.id,
                "message_id": message.id,
                "user_id": message.author.id,
                "reason": reason,
                "content": message.content,
                "embeds": [embed.to_dict() for embed in message.embeds],
                "warning_id": warning_msg.id if warning_msg else None,
                "report_id": report_msg.id,
                "user_banned": False,
                "removed": False,
            }
        )

    async def report_message(self, message: Message, reason: str) -> None:
        async with self._message_report_lock:
            if not (components := await self._generate_message_report(message, reason)):
                return None

            warning, report, attachment = components

            try:
                view = self.RemovalVoteView(self, message)
                warning_msg = await message.reply(
                    embed=warning, view=view, mention_author=False
                )
            except errors.Forbidden:
                warning_msg = None
                log.warning(f"Failed to send warning message in reply to {message.id}")

            report_channel = await self.bot.get_or_fetch_channel(
                cfg.discord.channels["report_scams"]
            )
            report_msg = await report_channel.send(embed=report, file=attachment)
            await self._add_message_report_to_db(
                message, reason, warning_msg, report_msg
            )

    async def manual_message_report(
        self, interaction: Interaction, message: Message
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if message.author.bot:
            return await interaction.followup.send(
                content="Bot messages can't be reported."
            )

        if message.author == interaction.user:
            return await interaction.followup.send(
                content="Did you just report yourself?"
            )

        async with self._message_report_lock:
            reason = f"Manual report by {interaction.user.mention}"
            if not (components := await self._generate_message_report(message, reason)):
                return await interaction.followup.send(
                    content="Failed to report message. It may have already been reported or deleted."
                )

            warning, report, attachment = components

            report_channel = await self.bot.get_or_fetch_channel(
                cfg.discord.channels["report_scams"]
            )
            report_msg = await report_channel.send(embed=report, file=attachment)

            moderator = await self.bot.get_or_fetch_user(
                cfg.rocketpool.support.moderator_id
            )
            view = self.RemovalVoteView(self, message)
            warning_msg = await message.reply(
                content=f"{moderator.mention} {report_msg.jump_url}",
                embed=warning,
                view=view,
                mention_author=False,
            )
            await self._add_message_report_to_db(
                message, reason, warning_msg, report_msg
            )
            await interaction.followup.send(content="Thanks for reporting!")

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
        # Heavily percent-encoded domain
        if re.search(r"https?://[^\s]*(?:%[0-9a-fA-F]{2}){5}", message.content):
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

        default_reason = "There is no ticket system in this server."

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
            ),
            [("relay"), ("query", "question", "inquiry")],
            [("instant", "live"), "chat"],
            [("submit"), ("question", "issue", "query")],
        )
        if self.__txt_contains(txt, strong_keywords):
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
    def __txt_contains(txt: str, kw: list | tuple | str) -> bool:
        match kw:
            case str():
                return kw in txt
            case tuple():
                return any(map(lambda w: ScamDetection.__txt_contains(txt, w), kw))
            case list():
                return all(map(lambda w: ScamDetection.__txt_contains(txt, w), kw))
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

    def _bio_redirect(self, message: Message) -> str | None:
        if not message.content or len(message.content) > 300:
            return None
        txt = self._get_message_content(message)
        if any(kw in txt for kw in ("my bio", "my icon", "my profile", "my pfp")):
            return "Redirection to malicious profile link"
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

    async def _reaction_spam(self, reaction: Reaction, user: User) -> str | None:
        # user reacts to their own message multiple times in quick succession to draw attention
        # check if user is a bot
        if user.bot:
            log.debug(f"Ignoring reaction by bot {user.id}")
            return None

        # check if the reaction is by the same user that created the message
        if reaction.message.author != user:
            log.debug(f"Ignoring reaction by non-author {user.id}")
            return None

        # check if the message is new enough (we ignore any reactions on messages older than 5 minutes)
        if (reaction.message.created_at - datetime.now(UTC)) > timedelta(minutes=5):
            log.debug(f"Ignoring reaction on old message {reaction.message.id}")
            return None

        # get all reactions on message
        reactions = self._message_react_cache.get(reaction.message.id)
        if reactions is None:
            reactions = {}
            for msg_reaction in reaction.message.reactions:
                reactions[msg_reaction.emoji] = {
                    user async for user in msg_reaction.users()
                }
            self._message_react_cache[reaction.message.id] = reactions
        elif reaction.emoji not in reactions:
            reactions[reaction.emoji] = {user}
        else:
            reactions[reaction.emoji].add(user)

        reaction_count = len(
            [r for r in reactions.values() if user in r and len(r) == 1]
        )
        log.debug(f"{reaction_count} reactions on message {reaction.message.id}")
        # if there are 8 reactions done by the author of the message, report it
        return "Reaction spam by message author" if (reaction_count >= 8) else None

    @Cog.listener()
    async def on_message(self, message: Message) -> None:
        log.debug(
            f"Message(id={message.id}, author={message.author}, channel={message.channel},"
            f' content="{message.content}", embeds={message.embeds})'
        )

        if message.author.bot:
            log.warning("Ignoring message sent by bot")
            return

        if self.is_reputable(message.author):
            log.warning(f"Ignoring message sent by trusted user ({message.author})")
            return

        if message.guild is None:
            return

        if message.guild.id != cfg.rocketpool.support.server_id:
            log.warning(f"Ignoring message in {message.guild.id})")
            return

        checks = [
            self._obfuscated_url,
            self._ticket_system,
            self._suspicious_x_account,
            self._suspicious_link,
            self._discord_invite,
            self._tap_on_this,
            self._bio_redirect,
            self._spam_wall,
        ]
        for check in checks:
            if reason := check(message):
                await self.report_message(message, reason)
                return

    @Cog.listener()
    async def on_message_edit(self, before: Message, after: Message) -> None:
        await self.on_message(after)

    @Cog.listener()
    async def on_reaction_add(self, reaction: Reaction, user: User) -> None:
        if reaction.message.guild.id != cfg.rocketpool.support.server_id:
            log.warning(f"Ignoring reaction in {reaction.message.guild.id}")
            return

        checks = [self._reaction_spam(reaction, user)]
        for reason in await asyncio.gather(*checks):
            if reason:
                await self.report_message(reaction.message, reason)
                return

    @Cog.listener()
    async def on_raw_message_delete(self, event: RawMessageDeleteEvent) -> None:
        await self._on_message_delete(event.message_id)

    @Cog.listener()
    async def on_raw_bulk_message_delete(
        self, event: RawBulkMessageDeleteEvent
    ) -> None:
        await asyncio.gather(
            *[self._on_message_delete(msg_id) for msg_id in event.message_ids]
        )

    async def _on_message_delete(self, message_id: int) -> None:
        async with self._message_report_lock:
            db_filter = {"type": "message", "message_id": message_id, "removed": False}
            if not (report := await self.bot.db.scam_reports.find_one(db_filter)):
                return

            channel = await self.bot.get_or_fetch_channel(report["channel_id"])
            with contextlib.suppress(
                errors.NotFound, errors.Forbidden, errors.HTTPException
            ):
                message = await channel.fetch_message(report["warning_id"])
                await message.delete()

            await self._update_report(report, "Original message has been deleted.")
            await self.bot.db.scam_reports.update_one(
                db_filter, {"$set": {"warning_id": None, "removed": True}}
            )

    @Cog.listener()
    async def on_member_ban(self, guild: Guild, user: User) -> None:
        async with (
            self._message_report_lock,
            self._thread_report_lock,
            self._user_report_lock,
        ):
            reports = await self.bot.db.scam_reports.find(
                {"guild_id": guild.id, "user_id": user.id, "user_banned": False}
            ).to_list(None)
            for report in reports:
                await self._update_report(report, "User has been banned.")
                await self.bot.db.scam_reports.update_one(
                    report, {"$set": {"user_banned": True}}
                )

    async def _update_report(self, report: dict, note: str) -> None:
        report_channel = await self.bot.get_or_fetch_channel(
            cfg.discord.channels["report_scams"]
        )
        try:
            message = await report_channel.fetch_message(report["report_id"])
            embed = message.embeds[0]
            embed.description += f"\n\n**{note}**"
            embed.color = (
                self.Color.WARN if (embed.color == self.Color.ALERT) else self.Color.OK
            )
            await message.edit(embed=embed)
        except Exception as e:
            await self.bot.report_error(e)

    async def report_thread(self, thread: Thread, reason: str) -> None:
        async with self._thread_report_lock:
            if not (components := await self._generate_thread_report(thread, reason)):
                return None

            warning, report = components

            try:
                view = self.RemovalVoteView(self, thread)
                warning_msg = await thread.send(embed=warning, view=view)
            except errors.Forbidden:
                log.warning(f"Failed to send warning message in thread {thread.id}")
                warning_msg = None

            report_channel = await self.bot.get_or_fetch_channel(
                cfg.discord.channels["report_scams"]
            )
            report_msg = await report_channel.send(embed=report)
            await self.bot.db.scam_reports.insert_one(
                {
                    "type": "thread",
                    "guild_id": thread.guild.id,
                    "channel_id": thread.id,
                    "user_id": thread.owner_id,
                    "reason": reason,
                    "content": thread.name,
                    "warning_id": warning_msg.id if warning_msg else None,
                    "report_id": report_msg.id,
                    "user_banned": False,
                    "removed": False,
                }
            )

    @Cog.listener()
    async def on_thread_create(self, thread: Thread) -> None:
        if thread.guild.id != cfg.rocketpool.support.server_id:
            log.warning(f"Ignoring thread creation in {thread.guild.id}")
            return

        lower = thread.name.strip().lower()
        scam_thread = (
            # Ticket emoji or "assistance" — always scam
            any(kw in lower for kw in ("🎫", "🎟️", "assistance"))
            # "ticket"/"tick" — no real ticket system
            or "tick" in lower
            # "support" — only in short names (long ones are legit discussions)
            or ("support" in lower and len(thread.name.strip()) < 25)
            # Dash-digits near end of name (scam: "user-0816"; skip: "RIP-1559: ...")
            or (
                (m := re.search(r"(-|–|—)\d{3,}", thread.name))  # noqa: RUF001
                and (
                    m.end() >= len(thread.name.strip()) - 2
                    or len(thread.name.strip()) < 30
                )
            )
            # Exact suspicious names
            or lower in (".", "!", "///")
        )
        if scam_thread:
            await self.report_thread(thread, "Illegitimate support thread")
            return

        log.debug(f"Ignoring thread creation (id: {thread.id}, name: {thread.name})")

    @Cog.listener()
    async def on_raw_thread_update(self, event: RawThreadUpdateEvent) -> None:
        thread: Thread = await self.bot.get_or_fetch_channel(event.thread_id)
        await self.on_thread_create(thread)

    @Cog.listener()
    async def on_raw_thread_delete(self, event: RawThreadDeleteEvent) -> None:
        db_filter = {"type": "thread", "channel_id": event.thread_id, "removed": False}
        async with self._thread_report_lock:
            if report := await self.bot.db.scam_reports.find_one(db_filter):
                await self._update_report(report, "Thread has been deleted.")
                await self.bot.db.scam_reports.update_one(
                    db_filter, {"$set": {"warning_id": None, "removed": True}}
                )

    @command()
    @guilds(cfg.rocketpool.support.server_id)
    async def report_user(self, interaction: Interaction, user: Member) -> None:
        """Generate a suspicious user report and send it to the report channel"""
        await self.manual_user_report(interaction, user)

    async def manual_user_report(self, interaction: Interaction, user: Member) -> None:
        await interaction.response.defer(ephemeral=True)

        if user.bot:
            return await interaction.followup.send(content="Bots can't be reported.")

        if user == interaction.user:
            return await interaction.followup.send(
                content="Did you just report yourself?"
            )

        async with self._user_report_lock:
            reason = f"Manual report by {interaction.user.mention}"
            if not (report := await self._generate_user_report(user, reason)):
                return await interaction.followup.send(
                    content="Failed to report user. They may have already been reported or banned."
                )

            report_channel = await self.bot.get_or_fetch_channel(
                cfg.discord.channels["report_scams"]
            )
            report_msg = await report_channel.send(embed=report)
            await self.bot.db.scam_reports.insert_one(
                {
                    "type": "user",
                    "guild_id": user.guild.id,
                    "user_id": user.id,
                    "reason": reason,
                    "content": user.display_name,
                    "warning_id": None,
                    "report_id": report_msg.id,
                    "user_banned": False,
                }
            )
            await interaction.followup.send(content="Thanks for reporting!")

    async def _generate_user_report(self, user: Member, reason: str) -> Embed | None:
        if not isinstance(user, Member):
            return None

        if await self.bot.db.scam_reports.find_one(
            {"type": "user", "guild_id": user.guild.id, "user_id": user.id}
        ):
            log.info(f"Found existing report for user {user.id} in database")
            return None

        report = Embed(title="🚨 Suspicious User Detected")
        report.color = self.Color.ALERT
        report.description = f"**Reason**: {reason}\n"
        report.description += (
            "\n"
            f"Name:  `{user.display_name}`\n"
            f"ID:    `{user.id}` ({user.mention})\n"
            f"Roles: [{', '.join(role.mention for role in user.roles[1:])}]\n"
            "\n"
            "Please review and take appropriate action."
        )
        report.set_thumbnail(url=user.display_avatar.url)
        return report


async def setup(bot):
    await bot.add_cog(ScamDetection(bot))
