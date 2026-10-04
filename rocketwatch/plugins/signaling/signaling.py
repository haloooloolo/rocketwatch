import logging
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

import aiohttp
import termplotlib as tpl
from discord import Interaction
from discord.app_commands import command
from eth_typing import BlockNumber, ChecksumAddress
from pymongo import DeleteMany, DeleteOne, InsertOne, ReplaceOne, UpdateOne
from web3 import Web3

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.block_time import ts_to_block
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import Embed, el_explorer_url, rocketdash_url
from rocketwatch.utils.event import Event, EventPlugin
from rocketwatch.utils.image import Color, FontVariant, Image, ImageCanvas
from rocketwatch.utils.readable import pretty_time
from rocketwatch.utils.retry import retry
from rocketwatch.utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.signaling")


@retry(tries=3, delay=1)
async def _query_api(endpoint: str, **params: str) -> Any:
    params["network"] = cfg.rocketpool.chain
    async with (
        aiohttp.ClientSession() as session,
        session.get(
            f"https://rocketdash.net/api/gov/{endpoint}", params=params
        ) as resp,
    ):
        resp.raise_for_status()
        return await resp.json()


@dataclass(frozen=True)
class Proposal:
    id: str
    title: str
    vote_type: str
    choices: list[str]
    start: int
    end: int
    state: str
    lifecycle: str
    scores: list[float]
    scores_total: float
    quorum: float

    _LABEL_SIZE = 20
    _TEXT_SIZE = 25
    _HEADER_SIZE = 30
    _TITLE_SIZE = 38
    _BAR_SIZE = 30

    _V_SPACE_SMALL = 10
    _V_SPACE_MEDIUM = 20
    _V_SPACE_LARGE = 40

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Proposal":
        return cls(
            id=data["proposal_id"],
            title=data["title"],
            vote_type=data["vote_type"],
            choices=data["choices"],
            start=data["start_time"],
            end=data["end_time"],
            state=data["state"],
            lifecycle=data["lifecycle"],
            scores=data["scores"],
            scores_total=data["scores_total"],
            quorum=data["quorum"],
        )

    def is_active(self) -> bool:
        return (
            self.state == "active"
            and self.lifecycle == "ready"
            and self.end >= datetime.now().timestamp()
        )

    def _predict_choice_height(self) -> int:
        return self._TEXT_SIZE + self._V_SPACE_SMALL + self._BAR_SIZE

    def predict_render_height(self, with_title: bool = True) -> int:
        height = 0
        if with_title:
            height = self._TITLE_SIZE + self._V_SPACE_LARGE
        height += len(self.choices) * (
            self._predict_choice_height() + self._V_SPACE_MEDIUM
        )
        height += self._V_SPACE_SMALL + self._HEADER_SIZE + self._V_SPACE_SMALL
        height += self._BAR_SIZE + self._V_SPACE_LARGE
        height += self._TEXT_SIZE
        return int(height)

    def reached_quorum(self) -> bool:
        return self.scores_total >= self.quorum

    def render_to(
        self,
        canvas: ImageCanvas,
        width: int,
        x_offset: int = 0,
        y_offset: int = 0,
        *,
        include_title: bool = True,
    ) -> int:
        def safe_div(x: float, y: float) -> float:
            return (x / y) if y else 0

        label_offset = self._BAR_SIZE / 2
        label_font_variant = FontVariant.BOLD

        def render_choice(
            _choice: str, _score: float, _x_offset: int, _y_offset: int
        ) -> int:
            color: Color = (128, 128, 128)  # slate gray
            choice_colors = {
                "for": (4, 99, 7),  # green
                "against": (156, 0, 47),  # red
                "abstain": (114, 121, 138),
            }
            for k, v in choice_colors.items():
                # assign color based on keywords
                if re.match(rf"^{k}\b", _choice.lower()):
                    color = v
                    break

            choice_height = 0
            canvas.dynamic_text(
                (_x_offset, _y_offset),
                _choice,
                self._TEXT_SIZE,
                max_width=(width * 3 / 4),
                anchor="lt",
            )
            choice_height += self._TEXT_SIZE + self._V_SPACE_SMALL

            divisor = max(self.scores) if len(self.scores) >= 5 else sum(self.scores)
            canvas.progress_bar(
                (_x_offset, _y_offset + choice_height),
                (width, self._BAR_SIZE),
                safe_div(_score, divisor),
                fill_color=color,
            )
            canvas.dynamic_text(
                (
                    _x_offset + label_offset,
                    _y_offset + choice_height + (self._BAR_SIZE / 2),
                ),
                f"{safe_div(_score, sum(self.scores)):.2%}",
                self._LABEL_SIZE,
                font_variant=label_font_variant,
                max_width=((width / 2) - label_offset),
                anchor="lm",
            )
            canvas.dynamic_text(
                (
                    _x_offset + width - label_offset,
                    _y_offset + choice_height + (self._BAR_SIZE / 2),
                ),
                f"{_score:,.2f}",
                self._LABEL_SIZE,
                font_variant=label_font_variant,
                max_width=((width / 2) - label_offset),
                anchor="rm",
            )
            choice_height += self._BAR_SIZE
            return choice_height

        proposal_height = 0

        if include_title:
            canvas.dynamic_text(
                (x_offset + (width / 2), y_offset),
                self.title,
                self._TITLE_SIZE,
                max_width=width,
                anchor="mt",
            )
            proposal_height += self._TITLE_SIZE + self._V_SPACE_LARGE

        # order (choice, score) pairs by score
        choice_scores = list(zip(self.choices, self.scores, strict=False))
        choice_scores.sort(key=lambda x: x[1], reverse=True)
        for choice, score in choice_scores:
            proposal_height += render_choice(
                choice, score, x_offset, y_offset + proposal_height
            )
            proposal_height += self._V_SPACE_MEDIUM

        proposal_height += self._V_SPACE_SMALL

        # quorum header
        canvas.dynamic_text(
            (x_offset, y_offset + proposal_height),
            "Quorum",
            self._HEADER_SIZE,
            max_width=(width / 2),
            anchor="lt",
        )
        proposal_height += self._HEADER_SIZE + self._V_SPACE_SMALL
        quorum_perc: float = safe_div(self.scores_total, self.quorum)

        # dark gray, turns white with inverted labels when quorum is met
        pb_color = (223, 223, 223) if (quorum_perc >= 1) else (82, 81, 80)
        label_color = (0, 0, 0) if (quorum_perc >= 1) else (255, 255, 255)
        canvas.progress_bar(
            (x_offset, y_offset + proposal_height),
            (width, self._BAR_SIZE),
            min(quorum_perc, 1),
            fill_color=pb_color,
        )
        canvas.dynamic_text(
            (
                x_offset + label_offset,
                y_offset + proposal_height + (self._BAR_SIZE / 2),
            ),
            f"{quorum_perc:.2%}",
            self._LABEL_SIZE,
            font_variant=label_font_variant,
            max_width=((width / 2) - label_offset),
            anchor="lm",
            color=label_color,
        )
        canvas.dynamic_text(
            (
                x_offset + width - label_offset,
                y_offset + proposal_height + (self._BAR_SIZE / 2),
            ),
            f"{self.scores_total:,.0f} / {self.quorum:,.0f}",
            self._LABEL_SIZE,
            font_variant=label_font_variant,
            max_width=((width / 2) - label_offset),
            anchor="rm",
            color=label_color,
        )
        proposal_height += self._BAR_SIZE + self._V_SPACE_LARGE

        # show remaining time until the vote ends
        rem_time = self.end - datetime.now().timestamp()
        canvas.dynamic_text(
            (x_offset + (width / 2), y_offset + proposal_height),
            f"{pretty_time(rem_time)} left" if (rem_time >= 0) else "Final Result",
            self._TEXT_SIZE,
            max_width=width,
            anchor="mt",
        )
        proposal_height += self._TEXT_SIZE
        return proposal_height

    @property
    def url(self) -> str:
        return rocketdash_url(f"vote/{self.id}")

    def get_embed_template(self) -> Embed:
        embed = Embed()
        embed.set_author(name="🔗 Data from rocketdash.net/vote", url=self.url)
        return embed

    def create_image(self, *, include_title: bool) -> Image:
        pad_top, pad_bottom = 20, 20
        pad_left, pad_right = 20, 20
        width = 800
        height = self.predict_render_height(include_title)
        canvas = ImageCanvas(
            width + pad_left + pad_right, height + pad_top + pad_bottom
        )
        self.render_to(canvas, width, pad_left, pad_top, include_title=include_title)
        return canvas.image

    async def create_start_event(self) -> Event:
        embed = self.get_embed_template()
        embed.title = ":bulb: New Signaling Vote"
        return Event(
            embed=embed,
            topic="signaling",
            block_number=await ts_to_block(self.start),
            event_name="pdao_signaling_vote_start",
            unique_id=f"signaling_vote_start:{self.id}",
            image=self.create_image(include_title=True),
        )

    def create_reached_quorum_event(self, block_number: BlockNumber) -> Event:
        embed = self.get_embed_template()
        embed.title = ":checkered_flag: Proposal Reached Quorum"
        return Event(
            embed=embed,
            topic="signaling",
            block_number=block_number,
            event_name="pdao_signaling_vote_quorum",
            unique_id=f"signaling_vote_quorum:{self.id}",
            image=self.create_image(include_title=True),
        )

    async def create_end_event(self) -> Event:
        max_for, max_against = 0.0, 0.0
        for choice, score in zip(self.choices, self.scores, strict=False):
            if "against" in choice.lower():
                max_against = max(max_against, score)
            elif "abstain" not in choice.lower():
                max_for = max(max_for, score)

        embed = self.get_embed_template()
        if self.reached_quorum() and (max_for >= max_against):
            embed.title = ":white_check_mark: Signaling Vote Passed"
        else:
            embed.title = ":x: Signaling Vote Failed"

        return Event(
            embed=embed,
            topic="signaling",
            block_number=await ts_to_block(self.end),
            event_name="pdao_signaling_vote_end",
            unique_id=f"signaling_vote_end:{self.id}",
            image=self.create_image(include_title=True),
        )


type SingleChoice = int
type MultiChoice = list[int]
# weighted votes use strings as keys
type WeightedChoice = dict[str, int]
type Choice = SingleChoice | MultiChoice | WeightedChoice


@dataclass(frozen=True, slots=True)
class Vote:
    proposal: Proposal
    node: ChecksumAddress
    signer: ChecksumAddress
    timestamp: int
    power: float
    choice: Choice
    reason: str
    is_override: bool

    @classmethod
    def from_api(cls, proposal: Proposal, data: dict[str, Any]) -> "Vote":
        return cls(
            proposal=proposal,
            node=Web3.to_checksum_address(data["node"]),
            signer=Web3.to_checksum_address(data["signer"]),
            timestamp=data["timestamp"],
            power=data["effective_power"],
            choice=data["choice"],
            reason=data["reason"] or "",
            is_override=data["is_override"],
        )

    @classmethod
    def from_db(cls, proposal: Proposal, doc: dict[str, Any]) -> "Vote":
        return cls(
            proposal=proposal,
            node=doc["node"],
            signer=doc["signer"],
            timestamp=doc["timestamp"],
            power=doc["power"],
            choice=doc["choice"],
            reason=doc["reason"],
            is_override=doc["is_override"],
        )

    def to_db(self) -> dict[str, Any]:
        doc = {k: v for k, v in asdict(self).items() if k != "proposal"}
        return {
            "_id": f"{self.proposal.id}:{self.node}",
            "proposal_id": self.proposal.id,
        } | doc

    def pretty_print(self) -> str | None:
        match raw_choice := self.choice:
            case int():
                return self._format_single_choice(raw_choice)
            case list():
                return self._format_multiple_choice(raw_choice)
            case dict():
                return self._format_weighted_choice(raw_choice)
            case _:
                log.error(f"Unknown vote type: {raw_choice}")
                return None

    def _label_choice(self, raw_vote: SingleChoice) -> str:
        # vote choice represented as 1-based index
        return self.proposal.choices[raw_vote - 1]

    def _format_single_choice(self, choice: SingleChoice) -> str:
        label = self._label_choice(choice)
        match label.lower():
            case "for":
                label = "✅ For"
            case "against":
                label = "❌ Against"
            case "abstain":
                label = "⚪ Abstain"
        return f"`{label}`"

    def _format_multiple_choice(self, choice: MultiChoice) -> str:
        labels = [self._label_choice(c) for c in choice]
        if len(labels) == 1:
            return f"`{labels[0]}`"
        if self.proposal.vote_type == "ranked-choice":
            lines = [f"{i}. {c}" for i, c in enumerate(labels, start=1)]
        else:
            lines = [f"- {c}" for c in labels]
        return "**" + "\n".join(lines) + "**"

    def _format_weighted_choice(self, choice: WeightedChoice) -> str:
        labels = {self._label_choice(int(c)): w for c, w in choice.items()}
        total_weight = sum(labels.values())
        choice_perc = [(c, round(100 * w / total_weight)) for c, w in labels.items()]
        choice_perc.sort(key=lambda x: x[1], reverse=True)
        graph = tpl.figure()
        graph.barh(
            [x[1] for x in choice_perc],
            [x[0] for x in choice_perc],
            force_ascii=True,
            max_width=15,
        )
        return "```" + str(graph.get_string()).replace("]", "%]") + "```"

    async def create_event(self, prev_vote: "Vote | None") -> Event | None:
        voter = await el_explorer_url(self.node)
        signer = await el_explorer_url(self.signer)

        vote_fmt = self.pretty_print()
        if vote_fmt is None:
            return None

        embed = self.proposal.get_embed_template()
        embed.title = f":ballot_box: {self.proposal.title}"

        if prev_vote is None:
            separator = " " if (len(vote_fmt) <= 30) else "\n"
            action = (
                "overrode their delegate and voted" if self.is_override else "voted"
            )
            embed.description = separator.join([f"{voter} {action}", vote_fmt])
        elif self.choice != prev_vote.choice:
            prev_vote_fmt = prev_vote.pretty_print()
            if prev_vote_fmt is None:
                return None
            parts = [
                f"{voter} changed their vote from",
                prev_vote_fmt,
                "to",
                vote_fmt,
            ]
            separator = " " if (len(vote_fmt) + len(prev_vote_fmt) <= 20) else "\n"
            embed.description = separator.join(parts)
        elif self.reason != prev_vote.reason:
            embed.description = (
                f"{voter} changed the reason for their vote"
                if prev_vote.reason
                else f"{voter} added context to their vote ({vote_fmt})"
                if (len(vote_fmt) <= 20)
                else f"{voter} added context to their vote:\n{vote_fmt}"
            )
        else:
            log.debug("Same vote as before, skipping event")
            return None

        if self.reason:
            max_length = 2000
            reason = self.reason
            if len(embed.description) + len(reason) > max_length:
                suffix = "..."
                overage = len(embed.description) + len(reason) - max_length
                reason = reason[: -(overage + len(suffix))] + suffix

            embed.description += f" ```{reason}```"

        embed.add_field(name="Signer", value=signer)
        embed.add_field(name="Voting Power", value=f"{self.power:,.2f}")
        embed.add_field(name="Timestamp", value=f"<t:{self.timestamp}:R>")

        unique_id = f"signaling_vote:{self.proposal.id}:{self.node}:{self.timestamp}"
        if self.power >= 250:
            return Event(
                embed=embed,
                topic="signaling",
                block_number=await ts_to_block(self.timestamp),
                unique_id=unique_id,
                event_name="pdao_signaling_vote",
                image=self.proposal.create_image(include_title=False),
            )
        else:
            return Event(
                embed=embed,
                topic="signaling",
                block_number=await ts_to_block(self.timestamp),
                unique_id=unique_id,
                event_name="signaling_vote",
                thumbnail=self.proposal.create_image(include_title=False),
            )


class Signaling(EventPlugin):
    def __init__(self, bot: RocketWatch):
        super().__init__(bot, timedelta(minutes=2))
        self.proposal_db = bot.db.signaling_proposals
        self.vote_db = bot.db.signaling_votes

    @staticmethod
    async def fetch_proposals() -> list[Proposal]:
        response = await _query_api("proposals")
        proposals = [Proposal.from_api(d) for d in response["proposals"]]
        return sorted(proposals, key=lambda p: p.start, reverse=True)

    @staticmethod
    async def fetch_active_proposals() -> list[Proposal]:
        return [p for p in await Signaling.fetch_proposals() if p.is_active()]

    @staticmethod
    async def fetch_discussion_url(proposal_id: str) -> str | None:
        response = await _query_api("proposal-detail", id=proposal_id)
        return response.get("discussion_url") or None

    @staticmethod
    async def fetch_votes(proposal: Proposal) -> list[Vote]:
        response = await _query_api("proposal-votes", id=proposal.id)
        return [Vote.from_api(proposal, d) for d in response["votes"]]

    async def _get_new_events(self) -> list[Event]:
        events: list[Event] = []

        proposal_db_changes: list[
            InsertOne[dict[str, Any]] | UpdateOne | DeleteOne
        ] = []
        vote_db_changes: list[ReplaceOne[dict[str, Any]] | DeleteMany] = []

        proposals = {p.id: p for p in await self.fetch_proposals()}
        known_active_proposals: dict[str, dict[str, Any]] = {}

        async for stored_proposal in self.proposal_db.find():
            proposal_id = stored_proposal["_id"]
            proposal = proposals.get(proposal_id)
            if proposal is not None and proposal.is_active():
                known_active_proposals[proposal_id] = stored_proposal
                continue

            if proposal is None:
                log.info(f"Tracked proposal {proposal_id} was deleted")
            else:
                log.info(f"Found expired proposal: {proposal}")
                events.append(await proposal.create_end_event())
            proposal_db_changes.append(DeleteOne({"_id": proposal_id}))
            vote_db_changes.append(DeleteMany({"proposal_id": proposal_id}))

        for proposal in proposals.values():
            if not proposal.is_active():
                continue

            log.debug(f"Processing proposal {proposal}")
            # votes cast before we first saw the proposal are recorded silently
            announce_votes = proposal.id in known_active_proposals
            if not announce_votes:
                log.info(f"Found new proposal: {proposal}")
                events.append(await proposal.create_start_event())
                proposal_db_changes.append(
                    InsertOne({"_id": proposal.id, "quorum": proposal.reached_quorum()})
                )
            elif proposal.reached_quorum() and (
                not known_active_proposals[proposal.id]["quorum"]
            ):
                log.info(f"Proposal {proposal} has reached quorum")
                events.append(proposal.create_reached_quorum_event(self._pending_block))
                proposal_db_changes.append(
                    UpdateOne({"_id": proposal.id}, {"$set": {"quorum": True}})
                )

            stored_votes = {
                doc["node"]: doc
                async for doc in self.vote_db.find({"proposal_id": proposal.id})
            }
            for vote in await self.fetch_votes(proposal):
                stored_vote = stored_votes.get(vote.node)
                if stored_vote and stored_vote["timestamp"] >= vote.timestamp:
                    continue

                log.debug(f"Processing vote {vote}")
                if announce_votes:
                    prev_vote = (
                        Vote.from_db(proposal, stored_vote) if stored_vote else None
                    )
                    if vote_event := await vote.create_event(prev_vote):
                        events.append(vote_event)

                doc = vote.to_db()
                vote_db_changes.append(
                    ReplaceOne({"_id": doc["_id"]}, doc, upsert=True)
                )

        if proposal_db_changes:
            await self.proposal_db.bulk_write(proposal_db_changes)

        if vote_db_changes:
            await self.vote_db.bulk_write(vote_db_changes)

        return events

    @command()
    async def signaling_votes(self, interaction: Interaction) -> None:
        """Show currently active signaling votes"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        embed = Embed(title="Signaling Votes")
        embed.set_author(
            name="🔗 Data from rocketdash.net/vote", url=rocketdash_url("vote")
        )

        proposals = (await self.fetch_active_proposals())[::-1]
        if not proposals:
            embed.description = "No active proposals."
            return await interaction.followup.send(embed=embed)

        num_proposals = len(proposals)
        num_cols = min(math.ceil(math.sqrt(num_proposals)), 4)
        num_rows = math.ceil(num_proposals / num_cols)

        v_spacing = 120
        h_spacing = 80

        pad_top, pad_bottom = 20, 20
        pad_left, pad_right = 20, 20

        proposal_width = 800
        total_width = (proposal_width * num_cols) + h_spacing * (num_cols - 1)

        # could potentially be smarter about arranging proposals with different proportions
        total_height = v_spacing * (num_rows - 1)
        proposal_grid: list[list[Proposal]] = []
        for row_idx in range(num_rows):
            row = proposals[row_idx * num_cols : (row_idx + 1) * num_cols]
            proposal_grid.append(row)
            # row height is equal to height of its tallest proposal
            total_height += max(p.predict_render_height() for p in row)

        # make sure proportions don't become too skewed
        if total_width < total_height:
            proposal_width = (total_height - h_spacing * (num_cols - 1)) // num_cols
            total_width = (proposal_width * num_cols) + h_spacing * (num_cols - 1)

        canvas = ImageCanvas(
            total_width + pad_left + pad_right, total_height + pad_top + pad_bottom
        )

        # draw proposals in num_rows x num_cols grid
        y_offset = pad_top
        for row in proposal_grid:
            max_height = 0
            x_offset = pad_left

            for proposal in row:
                height = proposal.render_to(canvas, proposal_width, x_offset, y_offset)
                max_height = max(max_height, height)
                x_offset += proposal_width + h_spacing

            y_offset += max_height + v_spacing

        file = canvas.image.to_file("signaling.png")
        embed.set_image(url=f"attachment://{file.filename}")
        await interaction.followup.send(embed=embed, file=file)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(Signaling(bot))
