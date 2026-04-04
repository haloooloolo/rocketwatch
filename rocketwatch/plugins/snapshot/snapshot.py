import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Optional, cast

import aiohttp
import regex
import termplotlib as tpl
from discord import Interaction
from discord.app_commands import command
from eth_typing import BlockNumber, ChecksumAddress
from graphql_query import Argument, Operation, Query
from pymongo import DESCENDING, DeleteOne, InsertOne, UpdateOne
from web3.constants import ADDRESS_ZERO

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.block_time import ts_to_block
from rocketwatch.utils.embeds import Embed, el_explorer_url
from rocketwatch.utils.event import Event, EventPlugin
from rocketwatch.utils.image import Color, FontVariant, Image, ImageCanvas
from rocketwatch.utils.readable import pretty_time
from rocketwatch.utils.retry import retry
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.snapshot")


class Snapshot(EventPlugin):
    def __init__(self, bot: RocketWatch):
        super().__init__(bot, timedelta(minutes=2))
        self.proposal_db = bot.db.snapshot_proposals
        self.vote_db = bot.db.snapshot_votes

    @staticmethod
    @retry(tries=3, delay=1)
    async def _query_api(query: Query) -> list[dict] | dict:
        query_json = {"query": Operation(type="query", queries=[query]).render()}
        log.debug(f"Snapshot query: {query_json}")
        async with (
            aiohttp.ClientSession() as session,
            session.get("https://hub.snapshot.org/graphql", json=query_json) as resp,
        ):
            response = await resp.json()
        if "errors" in response:
            raise Exception(response["errors"])
        result: list[dict] | dict = response["data"][query.name]
        return result

    @dataclass(frozen=True)
    class Proposal:
        State = Literal["active", "closed"]

        id: str
        title: str
        choices: list[str]
        start: int
        end: int
        scores: list[float]
        quorum: int

        _LABEL_SIZE = 20
        _TEXT_SIZE = 25
        _HEADER_SIZE = 30
        _TITLE_SIZE = 38
        _BAR_SIZE = 30

        _V_SPACE_SMALL = 10
        _V_SPACE_MEDIUM = 20
        _V_SPACE_LARGE = 40

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
            return sum(self.scores) >= self.quorum

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
                    if regex.match(rf"^{k.lower()}\b", choice.lower()):
                        color = v
                        break

                choice_height = 0
                canvas.dynamic_text(
                    (_x_offset, _y_offset),
                    _choice,
                    self._TEXT_SIZE,
                    max_width=(width / 2),
                    anchor="lt",
                )
                choice_height += self._TEXT_SIZE + self._V_SPACE_SMALL

                divisor = (
                    max(self.scores) if len(self.scores) >= 5 else sum(self.scores)
                )
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
            quorum_perc: float = safe_div(sum(self.scores), self.quorum)

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
                f"{sum(self.scores):,.0f} / {self.quorum:,.0f}",
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
            return f"https://vote.rocketpool.net/#/proposal/{self.id}"

        def get_embed_template(self) -> Embed:
            embed = Embed()
            embed.set_author(name="🔗 Data from snapshot.org", url=self.url)
            return embed

        def create_image(self, *, include_title: bool) -> Image:
            pad_top, pad_bottom = 20, 20
            pad_left, pad_right = 20, 20
            width = 800
            height = self.predict_render_height(include_title)
            canvas = ImageCanvas(
                width + pad_left + pad_right, height + pad_top + pad_bottom
            )
            self.render_to(
                canvas, width, pad_left, pad_top, include_title=include_title
            )
            return canvas.image

        async def create_start_event(self) -> Event:
            embed = self.get_embed_template()
            embed.title = ":bulb: New Snapshot Proposal"
            return Event(
                embed=embed,
                topic="snapshot",
                block_number=await ts_to_block(self.start),
                event_name="pdao_snapshot_vote_start",
                unique_id=f"snapshot_vote_start:{self.id}",
                image=self.create_image(include_title=True),
            )

        def create_reached_quorum_event(self, block_number: BlockNumber) -> Event:
            embed = self.get_embed_template()
            embed.title = ":checkered_flag: Proposal Reached Quorum"
            return Event(
                embed=embed,
                topic="snapshot",
                block_number=block_number,
                event_name="pdao_snapshot_vote_quorum",
                unique_id=f"snapshot_vote_quorum:{self.id}",
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
                embed.title = ":white_check_mark: Snapshot Proposal Passed"
            else:
                embed.title = ":x: Snapshot Proposal Failed"

            return Event(
                embed=embed,
                topic="snapshot",
                block_number=await ts_to_block(self.end),
                event_name="pdao_snapshot_vote_end",
                unique_id=f"snapshot_vote_end:{self.id}",
                image=self.create_image(include_title=True),
            )

    type SingleChoice = int
    type MultiChoice = list[int]
    # weighted votes use strings as keys for some reason
    type WeightedChoice = dict[str, int]
    type Choice = SingleChoice | MultiChoice | WeightedChoice

    @dataclass(frozen=True, slots=True)
    class Vote:
        proposal: "Snapshot.Proposal"
        id: str
        voter: ChecksumAddress
        created: int
        vp: float
        choice: "Snapshot.Choice"
        reason: str

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

        def _label_choice(self, raw_vote: "Snapshot.SingleChoice") -> str:
            # vote choice represented as 1-based index
            return self.proposal.choices[raw_vote - 1]

        def _format_single_choice(self, choice: "Snapshot.SingleChoice") -> str:
            label = self._label_choice(choice)
            match label.lower():
                case "for":
                    label = "✅ For"
                case "against":
                    label = "❌ Against"
                case "abstain":
                    label = "⚪ Abstain"
            return f"`{label}`"

        def _format_multiple_choice(self, choice: "Snapshot.MultiChoice") -> str:
            labels = [self._label_choice(c) for c in choice]
            if len(labels) == 1:
                return f"`{labels[0]}`"
            return "**" + "\n".join([f"- {c}" for c in labels]) + "**"

        def _format_weighted_choice(self, choice: "Snapshot.WeightedChoice") -> str:
            labels = {self._label_choice(int(c)): w for c, w in choice.items()}
            total_weight = sum(labels.values())
            choice_perc = [
                (c, round(100 * w / total_weight)) for c, w in labels.items()
            ]
            choice_perc.sort(key=lambda x: x[1], reverse=True)
            graph = tpl.figure()
            graph.barh(
                [x[1] for x in choice_perc],
                [x[0] for x in choice_perc],
                force_ascii=True,
                max_width=15,
            )
            return "```" + str(graph.get_string()).replace("]", "%]") + "```"

        async def create_event(
            self, prev_vote: Optional["Snapshot.Vote"]
        ) -> Event | None:
            node = await rp.call("rocketSignerRegistry.signerToNode", self.voter)
            signer = await el_explorer_url(self.voter)
            voter = signer if (node == ADDRESS_ZERO) else await el_explorer_url(node)

            vote_fmt = self.pretty_print()
            if vote_fmt is None:
                return None

            embed = self.proposal.get_embed_template()
            embed.title = f":ballot_box: {self.proposal.title}"

            if prev_vote is None:
                separator = " " if (len(vote_fmt) <= 30) else "\n"
                embed.description = separator.join([f"{voter} voted", vote_fmt])
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
                    else f"added context to their vote ({vote_fmt})"
                    if (len(vote_fmt) <= 20)
                    else f":\n{vote_fmt}"
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
            embed.add_field(name="Voting Power", value=f"{self.vp:,.2f}")
            embed.add_field(name="Timestamp", value=f"<t:{self.created}:R>")

            if self.vp >= 250:
                conditional_args = {
                    "event_name": "pdao_snapshot_vote",
                    "image": self.proposal.create_image(include_title=False),
                }
            else:
                conditional_args = {
                    "event_name": "snapshot_vote",
                    "thumbnail": self.proposal.create_image(include_title=False),
                }

            return Event(
                embed=embed,
                topic="snapshot",
                block_number=await ts_to_block(self.created),
                unique_id=f"snapshot_vote:{self.proposal.id}:{self.voter}:{self.created}",
                **conditional_args,
            )

    @staticmethod
    async def fetch_proposal(proposal_id: str) -> Proposal | None:
        query = Query(
            name="proposal",
            arguments=[Argument(name="id", value=f'"{proposal_id}"')],
            fields=["id", "title", "choices", "start", "end", "scores", "quorum"],
        )
        response: dict = await Snapshot._query_api(query)
        return Snapshot.Proposal(**response) if response else None

    @staticmethod
    async def fetch_proposals(
        state: Proposal.State, *, reverse: bool = False, limit: int = 25, skip: int = 0
    ) -> list[Proposal]:
        query = Query(
            name="proposals",
            arguments=[
                Argument(name="first", value=limit),
                Argument(name="skip", value=skip),
                Argument(
                    name="where",
                    value=[
                        Argument(name="space_in", value=['"rocketpool-dao.eth"']),
                        Argument(name="state", value=f'"{state}"'),
                    ],
                ),
                Argument(name="orderBy", value='"created"'),
                Argument(name="orderDirection", value="desc" if reverse else "asc"),
            ],
            fields=["id", "title", "choices", "start", "end", "scores", "quorum"],
        )
        response: list[dict] = await Snapshot._query_api(query)
        return [Snapshot.Proposal(**d) for d in response]

    @staticmethod
    async def fetch_votes(
        proposal: Proposal,
        *,
        created_after: int = 0,
        reverse: bool = False,
        limit: int = 100,
        skip: int = 0,
    ) -> list[Vote]:
        query = Query(
            name="votes",
            arguments=[
                Argument(name="first", value=limit),
                Argument(name="skip", value=skip),
                Argument(
                    name="where",
                    value=[
                        Argument(name="proposal", value=f'"{proposal.id}"'),
                        Argument(name="created_gt", value=created_after),
                    ],
                ),
                Argument(name="orderBy", value='"created"'),
                Argument(name="orderDirection", value="desc" if reverse else "asc"),
            ],
            fields=["id", "voter", "created", "vp", "choice", "reason"],
        )
        response: list[dict] = await Snapshot._query_api(query)
        return [Snapshot.Vote(proposal=proposal, **d) for d in response]

    async def _get_new_events(self) -> list[Event]:
        now = datetime.now()
        events: list[Event] = []

        proposal_db_changes: list[InsertOne | UpdateOne | DeleteOne] = []
        vote_db_changes: list[InsertOne] = []

        known_active_proposals: dict[str, dict] = {}
        async for stored_proposal in self.proposal_db.find():
            if stored_proposal["end"] >= now.timestamp():
                known_active_proposals[stored_proposal["_id"]] = stored_proposal
            else:
                # stored proposal ended, emit event and delete from DB
                log.info(f"Found expired proposal: {stored_proposal}")
                # recover full proposal
                if proposal := await self.fetch_proposal(stored_proposal["_id"]):
                    event = await proposal.create_end_event()
                    proposal_db_changes.append(DeleteOne(stored_proposal))
                    events.append(event)

        active_proposals = await self.fetch_proposals("active")
        for proposal in active_proposals:
            log.debug(f"Processing proposal {proposal}")
            if proposal.id not in known_active_proposals:
                # not aware of this proposal yet, emit event and insert into DB
                log.info(f"Found new proposal: {proposal}")
                event = await proposal.create_start_event()
                proposal_dict = {
                    "_id": proposal.id,
                    "start": proposal.start,
                    "end": proposal.end,
                    "quorum": proposal.reached_quorum(),
                }
                proposal_db_changes.append(InsertOne(proposal_dict))
                known_active_proposals[proposal.id] = proposal_dict
                events.append(event)
            elif proposal.reached_quorum() and (
                not known_active_proposals[proposal.id]["quorum"]
            ):
                log.info(f"Proposal {proposal} has reached quorum")
                event = proposal.create_reached_quorum_event(self._pending_block)
                proposal_db_changes.append(
                    UpdateOne({"_id": proposal.id}, {"$set": {"quorum": True}})
                )
                events.append(event)

            try:
                last_vote_entry = (
                    await self.vote_db.find({"proposal_id": proposal.id})
                    .sort({"created": DESCENDING})
                    .limit(1)
                    .to_list()
                )
                last_vote_ts = last_vote_entry[0]["created"]
            except IndexError:
                last_vote_ts = 0

            current_votes: list[Snapshot.Vote] = await self.fetch_votes(
                proposal, created_after=last_vote_ts
            )
            for vote in current_votes:
                log.debug(f"Processing vote {vote}")

                try:
                    stored_vote = (
                        await self.vote_db.find(
                            {"proposal_id": proposal.id, "voter": vote.voter}
                        )
                        .sort({"created": DESCENDING})
                        .limit(1)
                        .to_list()
                    )[0]
                    prev_vote = Snapshot.Vote(
                        id=stored_vote["_id"],
                        proposal=proposal,
                        voter=stored_vote["voter"],
                        created=stored_vote["created"],
                        vp=stored_vote["vp"],
                        choice=stored_vote["choice"],
                        reason=stored_vote["reason"],
                    )
                except IndexError:
                    prev_vote = None

                event = await vote.create_event(prev_vote)
                if event is None:
                    continue

                events.append(event)
                db_update = InsertOne(
                    {
                        "_id": vote.id,
                        "proposal_id": vote.proposal.id,
                        "voter": vote.voter,
                        "created": vote.created,
                        "vp": vote.vp,
                        "choice": vote.choice,
                        "reason": vote.reason,
                    }
                )
                vote_db_changes.append(db_update)

        if proposal_db_changes:
            await self.proposal_db.bulk_write(proposal_db_changes)

        if vote_db_changes:
            await self.vote_db.bulk_write(vote_db_changes)

        return events

    @command()
    async def snapshot_votes(self, interaction: Interaction) -> None:
        """Show currently active Snapshot proposals"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))

        embed = Embed(title="Snapshot Proposals")
        embed.set_author(
            name="🔗 Data from snapshot.org", url="https://vote.rocketpool.net"
        )

        proposals = (await self.fetch_proposals("active", reverse=True))[::-1]
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
        proposal_grid: list[list[Snapshot.Proposal]] = []
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

        file = canvas.image.to_file("snapshot.png")
        embed.set_image(url=f"attachment://{file.filename}")
        await interaction.followup.send(embed=embed, file=file)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(Snapshot(bot))
