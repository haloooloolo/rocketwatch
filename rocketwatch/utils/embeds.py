import contextlib
import json
import logging
import math
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
import discord
import humanize
from aiocache import cached
from discord import Color, Interaction
from discord.types.embed import EmbedType
from ens import InvalidName
from eth_typing import BlockIdentifier, BlockNumber, ChecksumAddress, HexStr
from etherscan_labels import Addresses
from web3.constants import ADDRESS_ZERO
from web3.types import TxReceipt

from utils.block_time import block_to_ts
from utils.cached_ens import ens
from utils.config import cfg
from utils.readable import advanced_tnx_url, s_hex
from utils.retry import retry
from utils.rocketpool import rp
from utils.sea_creatures import get_sea_creature_for_address
from utils.shared_w3 import w3

log = logging.getLogger("rocketwatch.embeds")

_ADDRESS_NAMES: dict[str, str] = json.loads(
    (
        Path(__file__).resolve().parent.parent / "strings" / "addresses.en.json"
    ).read_text()
)


class Embed(discord.Embed):
    def __init__(
        self,
        *,
        color: int | Color | None = None,
        title: Any | None = None,
        type: EmbedType = "rich",
        url: Any | None = None,
        description: Any | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        if color is None:
            color = Color.from_rgb(226, 116, 57)
        super().__init__(
            color=color,
            title=title,
            type=type,
            url=url,
            description=description,
            timestamp=timestamp,
        )
        self.set_footer_parts([])

    def set_footer_parts(self, parts: list[str]) -> None:
        footer_parts = ["Created by 0xinvis.eth, Developed by haloooloolo.eth"]
        if cfg.rocketpool.chain != "mainnet":
            footer_parts.append(f"Chain: {cfg.rocketpool.chain.capitalize()}")
        footer_parts.extend(parts)
        self.set_footer(text=" · ".join(footer_parts))


#: Type for custom fields: ``(name, value, inline)``
EmbedField = tuple[str, str, bool]


async def build_small_event_embed(description: str, tx_hash: HexStr) -> Embed:
    """Create a compact one-line embed with a ``[tnx]`` link and empty footer."""
    tx_link = await el_explorer_url(tx_hash, name="[tnx]")
    desc = f"{description} {tx_link}"
    if cfg.rocketpool.chain != "mainnet":
        desc += f" ({cfg.rocketpool.chain.capitalize()})"
    embed = Embed(description=desc)
    embed.set_footer(text="")
    return embed


async def build_event_embed(
    *,
    tx_hash: HexStr,
    block_number: BlockNumber,
    fields: list[EmbedField] | None = None,
    **kwargs: Any,
) -> Embed:
    """Create an :class:`Embed` with custom fields and standard tx footer."""
    embed = Embed(**kwargs)

    for name, value, inline in fields or []:
        embed.add_field(name=name, value=value, inline=inline)

    el_explorer = cfg.execution_layer.explorer
    tx_link = await el_explorer_url(tx_hash)
    tx_advanced = advanced_tnx_url(tx_hash)
    embed.add_field(name="Transaction Hash", value=f"{tx_link}{tx_advanced}")
    embed.add_field(
        name="Block Number",
        value=f"[{block_number}]({el_explorer}/block/{block_number})",
    )
    ts = await block_to_ts(block_number)
    embed.add_field(name="Timestamp", value=f"<t:{ts}:R> (<t:{ts}:f>)", inline=False)
    return embed


async def build_rich_event_embed(
    *,
    tx_hash: HexStr,
    block_number: BlockNumber,
    receipt: TxReceipt | None = None,
    sender: ChecksumAddress | None = None,
    caller: ChecksumAddress | None = None,
    fields: list[EmbedField] | None = None,
    **kwargs: Any,
) -> Embed:
    """Create an :class:`Embed` with sender/caller, custom fields, and tx footer."""
    embed = Embed(**kwargs)

    for name, value, inline in fields or []:
        embed.add_field(name=name, value=value, inline=inline)

    el_explorer = cfg.execution_layer.explorer
    tx_link = await el_explorer_url(tx_hash)
    tx_advanced = advanced_tnx_url(tx_hash)
    embed.add_field(name="Transaction Hash", value=f"{tx_link}{tx_advanced}")

    if sender:
        sea = await get_sea_creature_for_address(w3.to_checksum_address(sender))
        sender_link = await el_explorer_url(sender, prefix=sea)
        if caller and caller != sender:
            caller_sea = await get_sea_creature_for_address(
                w3.to_checksum_address(caller)
            )
            caller_link = await el_explorer_url(caller, prefix=caller_sea)
            value = f"{caller_link} ({sender_link})"
        else:
            value = sender_link
        embed.add_field(name="Sender Address", value=value)

    embed.add_field(
        name="Block Number",
        value=f"[{block_number}]({el_explorer}/block/{block_number})",
    )
    ts = await block_to_ts(block_number)
    embed.add_field(name="Timestamp", value=f"<t:{ts}:R> (<t:{ts}:f>)", inline=False)

    if receipt is not None and cfg.rocketpool.chain == "mainnet":
        tnx_fee = receipt["gasUsed"] * receipt["effectiveGasPrice"]
        tnx_fee_usd = round(await rp.get_eth_usdc_price() * tnx_fee / 10**18, 2)
        if tnx_fee >= 10**15:
            fee_str = f"{round(tnx_fee / 10**18, 3):,} ETH ({tnx_fee_usd} USDC)"
        elif tnx_fee >= 10**9:
            fee_str = f"{round(tnx_fee / 10**9):,} Gwei ({tnx_fee_usd} USDC)"
        else:
            fee_str = f"{tnx_fee:,} Wei ({tnx_fee_usd} USDC)"
        embed.add_field(name="Transaction Fee", value=fee_str, inline=False)

    return embed


def format_value(value: int | float) -> str:
    """Format a numeric value for display: auto-decimal + comma separation."""
    if value:
        decimal = 5 - math.floor(math.log10(abs(value)))
        decimal = max(0, min(5, decimal))
        value = round(value, decimal)
    if value == int(value):
        value = int(value)
    return humanize.intcomma(value)


# Convert a user-provided string into a display name and address.
# If an ens name is provided, it will be used as the display name.
# If an address is provided, the display name will either be the reverse record or the address.
# If the user input isn't sanitary, send an error message back to the user and return None, None.
async def resolve_ens(
    interaction: Interaction, node_address: str
) -> tuple[str | None, ChecksumAddress | None]:
    # if it looks like an ens, attempt to resolve it
    if "." in node_address:
        try:
            address = await ens.resolve_name(node_address)
            if not address:
                await interaction.followup.send("ENS name not found")
                return None, None

            return node_address, address
        except InvalidName:
            await interaction.followup.send("Invalid ENS name")
            return None, None

    # if it's just an address, look for a reverse record
    try:
        address = w3.to_checksum_address(node_address)
    except Exception:
        await interaction.followup.send("Invalid address")
        return None, None

    try:
        display_name = await ens.get_name(node_address) or address
        return display_name, address
    except InvalidName:
        await interaction.followup.send("Invalid address")
        return None, None


_pdao_delegates: dict[str, str] = {}


@cached(ttl=900)
@retry(tries=3, delay=1)
async def get_pdao_delegates() -> dict[str, str]:
    global _pdao_delegates
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get("https://delegates.rocketpool.net/api/delegates") as resp,
        ):
            _pdao_delegates = {d["nodeAddress"]: d["name"] for d in await resp.json()}
    except Exception:
        log.warning("Failed to fetch pDAO delegates.")
    return _pdao_delegates


async def el_explorer_url(
    target: str,
    name: str = "",
    prefix: str | None = "",
    name_fmt: Callable[[str], str] | None = None,
    block: BlockIdentifier = "latest",
) -> str:
    _prefix = ""

    if w3.is_address(target):
        # sanitize address
        target = w3.to_checksum_address(target)
        url = f"{cfg.execution_layer.explorer}/address/{target}"

        chain = cfg.rocketpool.chain
        dashboard_network = "" if (chain == "mainnet") else f"?network={chain}"

        if not name and (n := _ADDRESS_NAMES.get(target)):
            name = n

        if await rp.is_node(target):
            megapool_address = await rp.call(
                "rocketNodeManager.getMegapoolAddress", target
            )
            if megapool_address != ADDRESS_ZERO:
                url = f"https://rocketdash.net/megapool/{megapool_address}{dashboard_network}"
            if await rp.call(
                "rocketNodeManager.getSmoothingPoolRegistrationState",
                target,
                block=block,
            ):
                _prefix += ":cup_with_straw:"
            if not name:
                if member_id := await rp.call(
                    "rocketDAONodeTrusted.getMemberID", target, block=block
                ):
                    _prefix += "🔮"
                    name = member_id
                elif member_id := await rp.call(
                    "rocketDAOSecurity.getMemberID", target, block=block
                ):
                    _prefix += "🔒"
                    name = member_id
                elif delegate_name := (await get_pdao_delegates()).get(target):
                    _prefix += "🏛️"
                    name = delegate_name
        elif await rp.is_megapool(target):
            url = f"https://rocketdash.net/megapool/{target}{dashboard_network}"
        elif await rp.is_minipool(target):
            if chain == "mainnet":
                url = f"https://rocketexplorer.net/validator/{target}"

        if not name and cfg.rocketpool.chain != "mainnet":
            name = s_hex(target)

        if not name:
            a = Addresses.get(target)
            # don't apply name if it has  label is one with the id "take-action", as these don't show up on the explorer
            if all(
                (
                    (
                        not a.labels
                        or len(a.labels) != 1
                        or a.labels[0].id != "take-action"
                    ),
                    a.name and ("alert" not in a.name.lower()),
                )
            ):
                name = a.name
        if not name:
            name = await ens.get_name(target)

        if code := await w3.eth.get_code(target):
            _prefix += "📄"
            if (not name) and (
                w3.keccak(text=code.hex()).hex() in cfg.other.mev_hashes
            ):
                name = "MEV Bot Contract"
            if not name:
                with contextlib.suppress(Exception):
                    c = w3.eth.contract(
                        address=target,
                        abi=[
                            {
                                "inputs": [],
                                "name": "name",
                                "outputs": [
                                    {
                                        "internalType": "string",
                                        "name": "",
                                        "type": "string",
                                    }
                                ],
                                "stateMutability": "view",
                                "type": "function",
                            }
                        ],
                    )
                    n = await c.functions.name().call()
                    # make sure nobody is trying to inject a custom link, as there was a guy that made the name of his contract
                    # 'RocketSwapRouter](https://etherscan.io/search?q=0x16d5a408e807db8ef7c578279beeee6b228f1c1c)[',
                    # in an attempt to get people to click on his contract

                    # first, if the name has a link in it, we ignore it
                    if any(
                        keyword in n.lower()
                        for keyword in [
                            "http",
                            "discord",
                            "airdrop",
                            "telegram",
                            "twitter",
                            "youtube",
                        ]
                    ):
                        log.warning(f"Contract {target} has a suspicious name: {n}")
                    else:
                        name = (
                            f"{discord.utils.remove_markdown(n, ignore_links=False)}*"
                        )
    else:
        # transaction hash
        url = f"{cfg.execution_layer.explorer}/tx/{target}"

    if not name:
        # fall back to shortened address
        name = s_hex(target)
    if name_fmt:
        name = name_fmt(name)

    prefix = "" if (prefix is None) else prefix + _prefix
    return f"{prefix}[{name}]({url})"
