"""Shared base for events built from chain data (transactions and logs)."""

from __future__ import annotations

import contextlib
import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar, Literal, TypedDict

from discord import Interaction
from discord.ui import Modal, TextInput
from eth_typing import BlockNumber, ChecksumAddress, HexStr
from hexbytes import HexBytes
from web3.constants import ADDRESS_ZERO, HASH_ZERO
from web3.types import TxReceipt, Wei

from rocketwatch.utils.embeds import (
    Embed,
    build_event_embed,
    build_rich_event_embed,
    build_small_event_embed,
    el_explorer_url,
    format_value,
)
from rocketwatch.utils.type_markers import auto_format

DUMMY_RECEIPT: TxReceipt = {
    "blockHash": HexBytes(HASH_ZERO),
    "blockNumber": BlockNumber(0),
    "contractAddress": None,
    "cumulativeGasUsed": 0,
    "effectiveGasPrice": Wei(0),
    "gasUsed": 0,
    "from": ChecksumAddress(ADDRESS_ZERO),
    "logs": [],
    "logsBloom": HexBytes(b""),
    "root": HexStr(""),
    "status": 1,
    "to": ChecksumAddress(ADDRESS_ZERO),
    "transactionHash": HexBytes(HASH_ZERO),
    "transactionIndex": 0,
    "type": 0,
}


class NamedEmbeds(list[Embed]):
    """Embeds posted under a different event name than the handler's own,
    for handlers whose outcome decides the name (e.g. joined vs. left)."""

    def __init__(self, event_name: str, embeds: list[Embed]) -> None:
        super().__init__(embeds)
        self.event_name = event_name


class ChainEvent[DataT](ABC):
    """An event type: turns decoded chain data into Discord embeds.

    Subclasses declare their expected fields in a nested ``Args`` TypedDict,
    whose ``Annotated`` markers drive :meth:`_fmt`. Return ``[]`` from
    :meth:`build_embeds` to filter the event out.
    """

    event_name: str
    # contract addresses are re-resolved after this event (protocol upgrades)
    reloads_contracts: ClassVar[bool] = False

    async def resolve(
        self, args: dict[str, Any], event: DataT
    ) -> ChainEvent[DataT] | None:
        """Dispatch to the event type that handles *args*; ``None`` filters it out."""
        return self

    def args_type(self) -> type:
        """The TypedDict declaring this event's fields and formatting markers."""
        return type(self).Args  # type: ignore[attr-defined, no-any-return]

    async def _fmt(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Auto-format *args* using this event's Args TypedDict."""
        return dict(await auto_format(args, self.args_type()))

    @staticmethod
    async def embed(args: Mapping[str, Any], **kwargs: Any) -> Embed:
        """A full embed with the transaction footer."""
        return await build_event_embed(
            tx_hash=args["transactionHash"],
            block_number=args["blockNumber"],
            **kwargs,
        )

    @staticmethod
    async def rich_embed(
        args: Mapping[str, Any], receipt: TxReceipt, **kwargs: Any
    ) -> Embed:
        """A full embed with sender, fee and the transaction footer."""
        return await build_rich_event_embed(
            tx_hash=args["transactionHash"],
            block_number=args["blockNumber"],
            receipt=receipt,
            **kwargs,
        )

    @staticmethod
    async def line(args: Mapping[str, Any], text: str) -> Embed:
        """A one-line embed linking the transaction."""
        return await build_small_event_embed(text, args["transactionHash"])

    @abstractmethod
    async def build_embeds(
        self, args: Any, event: DataT, receipt: TxReceipt
    ) -> list[Embed]: ...


class TemplateEvent[DataT](ChainEvent[DataT]):
    """An event that is one sentence over its formatted fields, e.g.
    ``text="{node} has been slashed for **{amount} RPL**!"``.

    Amounts are formatted with ``format_value``. Fields in *before* are linked
    as of the block before the event, for members the event removed (their
    member name no longer resolves after it). Anything with conditions or
    lookups should be a class instead.
    """

    def __init__(
        self,
        event_name: str,
        text: str,
        *,
        fields: dict[str, Any] | None = None,
        title: str | None = None,
        style: Literal["embed", "line", "rich"] = "embed",
        image: str | None = None,
        before: tuple[str, ...] = (),
    ) -> None:
        self.event_name = event_name
        self._args: type = TypedDict(f"{event_name}_args", fields or {})  # type: ignore[misc]
        self._text = text
        self._title = title
        self._style = style
        self._image = image
        self._before = before

    def args_type(self) -> type:
        return self._args

    async def build_embeds(
        self, args: Any, event: DataT, receipt: TxReceipt
    ) -> list[Embed]:
        values = {
            k: format_value(v) if isinstance(v, float) else v
            for k, v in (await self._fmt(args)).items()
        }
        for name in self._before:
            values[name] = await el_explorer_url(
                args[name], block=args["blockNumber"] - 1
            )
        text = self._text.format(**values)

        if self._style == "line":
            embed = await self.line(args, text)
        elif self._style == "rich":
            embed = await self.rich_embed(
                args,
                receipt,
                sender=args.get("from"),
                caller=args.get("caller"),
                title=self._title,
                description=text,
            )
        else:
            embed = await self.embed(args, title=self._title, description=text)

        if self._image:
            embed.set_image(url=self._image)
        return [embed]


def posted_name(handler: ChainEvent[Any], embeds: list[Embed]) -> str:
    """The event name *embeds* are posted under."""
    if isinstance(embeds, NamedEmbeds):
        return embeds.event_name
    return handler.event_name


# ---------------------------------------------------------------------------
# Previews (owner commands that render an event from typed-in arguments)
# ---------------------------------------------------------------------------


def preview_fields(handler: ChainEvent[Any], context: type) -> list[tuple[str, bool]]:
    """``[(name, required), ...]`` for *handler*'s Args fields not in *context*."""
    args_type = handler.args_type()
    context_keys = set(context.__annotations__)
    return [
        (name, name in args_type.__required_keys__)  # type: ignore[attr-defined]
        for name in args_type.__annotations__
        if name not in context_keys
    ]


async def send_preview[DataT](
    interaction: Interaction,
    handler: ChainEvent[DataT],
    args: dict[str, Any],
    event: DataT,
) -> None:
    resolved = await handler.resolve(args, event)
    if resolved is None:
        await interaction.followup.send(content="Event filtered out.")
        return
    embeds = await resolved.build_embeds(args, event, DUMMY_RECEIPT)
    if embeds:
        await interaction.followup.send(embeds=embeds)
    else:
        await interaction.followup.send(content="No events triggered.")


class PreviewModal(Modal):
    """Asks for an event's argument values; JSON values are parsed."""

    def __init__(
        self,
        handler: ChainEvent[Any],
        fields: list[tuple[str, bool]],
        render: Callable[[Interaction, dict[str, Any]], Awaitable[None]],
    ) -> None:
        super().__init__(title=handler.event_name[:45])
        self.fields = fields
        self.render = render
        self.param_inputs: list[TextInput[PreviewModal]] = []
        for name, required in fields:
            text_input: TextInput[PreviewModal] = TextInput(
                label=name[:45], required=required
            )
            self.add_item(text_input)
            self.param_inputs.append(text_input)

    async def on_submit(self, interaction: Interaction) -> None:
        await interaction.response.defer()
        parsed: dict[str, Any] = {}
        for text_input, (name, _) in zip(self.param_inputs, self.fields, strict=True):
            if text_input.value:
                val: Any = text_input.value
                with contextlib.suppress(json.JSONDecodeError, ValueError):
                    val = json.loads(val)
                parsed[name] = val
        await self.render(interaction, parsed)
