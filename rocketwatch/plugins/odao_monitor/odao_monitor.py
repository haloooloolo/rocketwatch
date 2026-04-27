import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

import humanize
from discord import utils as discord_utils
from discord.abc import Messageable
from discord.ext import commands, tasks
from eth_typing import BlockNumber
from pymongo import UpdateOne

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import CustomColors, Embed, el_explorer_url
from rocketwatch.utils.event_logs import get_logs
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.shared_w3 import w3

log = logging.getLogger("rocketwatch.odao_monitor")

GRACE_PERIOD = timedelta(hours=1)
MEMBER_INACTIVITY = timedelta(days=7)
LAST_SEEN_LOOKBACK = timedelta(days=365)
BLOCK_TIME = timedelta(seconds=12)
RUN_AT = time(hour=12, minute=0, tzinfo=UTC)

META_ID = "_meta"


@dataclass(frozen=True, slots=True)
class Duty:
    id: str
    name: str
    contract_name: str
    event_name: str
    block_getter: str
    frequency_getter: str
    value_fields: tuple[str, ...]


DUTIES: list[Duty] = [
    Duty(
        id="balances",
        name="rETH Balance Update",
        contract_name="rocketNetworkBalances",
        event_name="BalancesSubmitted",
        block_getter="getBalancesBlock",
        frequency_getter="getSubmitBalancesFrequency",
        value_fields=(
            "block",
            "slotTimestamp",
            "totalEth",
            "stakingEth",
            "rethSupply",
        ),
    ),
    Duty(
        id="prices",
        name="RPL Price Update",
        contract_name="rocketNetworkPrices",
        event_name="PricesSubmitted",
        block_getter="getPricesBlock",
        frequency_getter="getSubmitPricesFrequency",
        value_fields=("block", "slotTimestamp", "rplPrice"),
    ),
]


async def _fetch_duty_state(duty: Duty) -> tuple[int, datetime, timedelta]:
    """Returns (last_consensus_block, last_consensus_dt, period)."""
    contract = await rp.get_contract_by_name(duty.contract_name)
    settings = await rp.get_contract_by_name("rocketDAOProtocolSettingsNetwork")
    last_block, period = await rp.multicall(
        [
            contract.functions[duty.block_getter](),
            settings.functions[duty.frequency_getter](),
        ]
    )
    last_ts = (await w3.eth.get_block(last_block))["timestamp"]
    return (
        last_block,
        datetime.fromtimestamp(last_ts, tz=UTC),
        timedelta(seconds=period),
    )


async def _add_pending_submissions_fields(
    embed: Embed,
    duty: Duty,
    last_consensus_block: int,
    latest_block: int,
    members: list[str],
) -> None:
    """Append a field per vote group (and one for non-submitters) to the embed."""
    contract = await rp.get_contract_by_name(duty.contract_name)
    event = contract.events[duty.event_name]
    # cover the gap from last consensus to head, plus a small buffer
    lookback_blocks = (latest_block - last_consensus_block) + 1000
    from_block = max(0, latest_block - lookback_blocks)
    logs = await get_logs(
        event, BlockNumber(from_block), BlockNumber(latest_block), address_agnostic=True
    )
    pending = [log for log in logs if log["args"]["block"] > last_consensus_block]

    latest_per_member: dict[str, Any] = {}
    for entry in pending:
        addr = entry["args"]["from"]
        prev = latest_per_member.get(addr)
        if prev is None or entry["blockNumber"] > prev["blockNumber"]:
            latest_per_member[addr] = entry

    groups: dict[tuple[Any, ...], list[str]] = {}
    for addr, entry in latest_per_member.items():
        values = tuple(entry["args"][k] for k in duty.value_fields)
        groups.setdefault(values, []).append(addr)

    sorted_groups = sorted(groups.items(), key=lambda x: -len(x[1]))
    if len(sorted_groups) > 1:
        differing_fields = [
            i
            for i in range(len(duty.value_fields))
            if len({values[i] for values, _ in sorted_groups}) > 1
        ]
    else:
        differing_fields = list(range(len(duty.value_fields)))

    for idx, (values, addrs) in enumerate(sorted_groups, start=1):
        value_str = "\n".join(
            f"`{duty.value_fields[i]}` = `{values[i]}`" for i in differing_fields
        )
        addr_links = [await el_explorer_url(a) for a in addrs]
        member_lines = "\n".join(f"- {link}" for link in addr_links)
        embed.add_field(
            name=f"Submission Group {idx} ({len(addrs)} vote{'s' if len(addrs) != 1 else ''})",
            value=f"{value_str}\n{member_lines}",
            inline=False,
        )

    not_submitted = [m for m in members if m not in latest_per_member]
    if not_submitted:
        addr_links = [await el_explorer_url(a) for a in not_submitted]
        member_lines = "\n".join(f"- {link}" for link in addr_links)
        embed.add_field(
            name=f"No Submission ({len(not_submitted)})",
            value=member_lines,
            inline=False,
        )


class ODAOMonitor(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.collection = self.bot.db.odao_monitor
        self.task.start()

    async def cog_unload(self) -> None:
        self.task.cancel()

    async def _ingest_submissions(self, latest_block: int) -> None:
        meta = await self.collection.find_one({"_id": META_ID})
        if meta:
            from_block = meta["last_scanned_block"] + 1
        else:
            from_block = max(0, latest_block - int(LAST_SEEN_LOOKBACK / BLOCK_TIME))

        if from_block > latest_block:
            return

        balances = await rp.get_contract_by_name("rocketNetworkBalances")
        prices = await rp.get_contract_by_name("rocketNetworkPrices")
        bal_logs = await get_logs(
            balances.events.BalancesSubmitted,
            BlockNumber(from_block),
            BlockNumber(latest_block),
            address_agnostic=True,
        )
        price_logs = await get_logs(
            prices.events.PricesSubmitted,
            BlockNumber(from_block),
            BlockNumber(latest_block),
            address_agnostic=True,
        )

        ops: list[UpdateOne] = []
        for entries, field in (
            (bal_logs, "last_balance_block"),
            (price_logs, "last_price_block"),
        ):
            latest_by_addr: dict[str, int] = {}
            for entry in entries:
                addr = entry["args"]["from"]
                block = entry["blockNumber"]
                if block > latest_by_addr.get(addr, 0):
                    latest_by_addr[addr] = block
            for addr, block in latest_by_addr.items():
                ops.append(
                    UpdateOne({"_id": addr}, {"$max": {field: block}}, upsert=True)
                )
        if ops:
            await self.collection.bulk_write(ops)

        await self.collection.update_one(
            {"_id": META_ID},
            {"$set": {"last_scanned_block": latest_block}},
            upsert=True,
        )

    async def _get_member_addresses(self) -> list[str]:
        dao = await rp.get_contract_by_name("rocketDAONodeTrusted")
        member_count = await dao.functions.getMemberCount().call()
        addresses = await rp.multicall(
            [dao.functions.getMemberAt(i) for i in range(member_count)]
        )
        return [w3.to_checksum_address(addr) for addr in addresses]

    async def _get_inactive_members(
        self, latest_block: int
    ) -> tuple[list[tuple[str, BlockNumber]], list[tuple[str, BlockNumber]]]:
        """Returns (missed balances, missed prices) — list of (address, last_block)."""
        threshold_block = latest_block - int(MEMBER_INACTIVITY / BLOCK_TIME)
        addresses = await self._get_member_addresses()

        docs = await self.collection.find({"_id": {"$in": addresses}}).to_list(None)
        state = {d["_id"]: d for d in docs}

        missed_balances: list[tuple[str, BlockNumber]] = []
        missed_prices: list[tuple[str, BlockNumber]] = []
        for addr in addresses:
            entry = state.get(addr, {})
            bal_block = BlockNumber(entry.get("last_balance_block", 0))
            price_block = BlockNumber(entry.get("last_price_block", 0))
            if bal_block < threshold_block:
                missed_balances.append((addr, bal_block))
            if price_block < threshold_block:
                missed_prices.append((addr, price_block))
        missed_balances.sort(key=lambda item: item[1])
        missed_prices.sort(key=lambda item: item[1])
        return missed_balances, missed_prices

    @tasks.loop(time=RUN_AT)
    async def task(self) -> None:
        channel_id = cfg.discord.channels.get("monitor")
        if not channel_id:
            return

        channel = await self.bot.get_or_fetch_channel(channel_id)
        assert isinstance(channel, Messageable)

        now = datetime.now(tz=UTC)
        latest_block = await w3.eth.get_block_number()
        members = await self._get_member_addresses()

        for duty in DUTIES:
            last_block, last_update, period = await _fetch_duty_state(duty)
            deadline = last_update + period
            if now < deadline + GRACE_PERIOD:
                continue

            log.warning(
                "oDAO duty %s overdue: last=%s, deadline=%s, now=%s",
                duty.id,
                last_update.isoformat(),
                deadline.isoformat(),
                now.isoformat(),
            )
            embed = Embed(title="🚨 Lost Oracle Consensus", color=CustomColors.RED)
            embed.description = (
                f"The Oracle DAO has not performed the **{duty.name}** on time.\n\n"
                f"Last update: {discord_utils.format_dt(last_update, 'R')} "
                f"({discord_utils.format_dt(last_update, 'f')})\n"
                f"Expected every **{humanize.precisedelta(period)}**, "
                f"due {discord_utils.format_dt(deadline, 'R')}."
            )
            await _add_pending_submissions_fields(
                embed, duty, last_block, latest_block, members
            )
            await channel.send(embed=embed)

        await self._ingest_submissions(latest_block)

        # only assess inactive members on Monday
        if now.weekday() != 0:
            return

        missed_balances, missed_prices = await self._get_inactive_members(latest_block)
        if missed_balances or missed_prices:
            log.warning(
                "Inactive oDAO members: balances=%s, prices=%s",
                missed_balances,
                missed_prices,
            )
            unique_blocks = {b for _, b in missed_balances + missed_prices if b}
            block_data = await asyncio.gather(
                *(w3.eth.get_block(b) for b in unique_blocks)
            )
            block_ts = {b["number"]: b["timestamp"] for b in block_data}

            embed = Embed(title="⚠️ Inactive oDAO Members", color=CustomColors.YELLOW)
            for field_name, items in (
                ("Missed rETH Balance Update", missed_balances),
                ("Missed RPL Price Update", missed_prices),
            ):
                if not items:
                    continue
                lines = []
                for addr, block in items:
                    link = await el_explorer_url(addr)
                    if block and (block in block_ts):
                        last_dt = datetime.fromtimestamp(block_ts[block], tz=UTC)
                        suffix = discord_utils.format_dt(last_dt, "R")
                    else:
                        suffix = f">{LAST_SEEN_LOOKBACK.days}d ago"
                    lines.append(f"- {link}: last seen {suffix}")
                embed.add_field(
                    name=field_name,
                    value="\n".join(lines),
                    inline=False,
                )
            await channel.send(embed=embed)

    @task.before_loop
    async def before_task(self) -> None:
        await self.bot.wait_until_ready()

    @task.error
    async def on_task_error(self, err: BaseException) -> None:
        assert isinstance(err, Exception)
        await self.bot.report_error(err)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(ODAOMonitor(bot))
