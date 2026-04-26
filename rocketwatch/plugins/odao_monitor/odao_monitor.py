import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

import humanize
from discord import utils as discord_utils
from discord.abc import Messageable
from discord.ext import commands, tasks
from eth_typing import BlockNumber
from pymongo import UpdateOne

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import Embed, el_explorer_url
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
    fetch: Callable[[], Awaitable[tuple[datetime, timedelta]]]


async def _balances_duty() -> tuple[datetime, timedelta]:
    network_balances = await rp.get_contract_by_name("rocketNetworkBalances")
    settings = await rp.get_contract_by_name("rocketDAOProtocolSettingsNetwork")
    last_block, period = await rp.multicall(
        [
            network_balances.functions.getBalancesBlock(),
            settings.functions.getSubmitBalancesFrequency(),
        ]
    )
    last_ts = (await w3.eth.get_block(last_block))["timestamp"]
    return datetime.fromtimestamp(last_ts, tz=UTC), timedelta(seconds=period)


async def _prices_duty() -> tuple[datetime, timedelta]:
    network_prices = await rp.get_contract_by_name("rocketNetworkPrices")
    settings = await rp.get_contract_by_name("rocketDAOProtocolSettingsNetwork")
    last_block, period = await rp.multicall(
        [
            network_prices.functions.getPricesBlock(),
            settings.functions.getSubmitPricesFrequency(),
        ]
    )
    last_ts = (await w3.eth.get_block(last_block))["timestamp"]
    return datetime.fromtimestamp(last_ts, tz=UTC), timedelta(seconds=period)


DUTIES: list[Duty] = [
    Duty(id="balances", name="rETH Balance Update", fetch=_balances_duty),
    Duty(id="prices", name="RPL Price Update", fetch=_prices_duty),
]


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
        )
        price_logs = await get_logs(
            prices.events.PricesSubmitted,
            BlockNumber(from_block),
            BlockNumber(latest_block),
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

    async def _get_inactive_members(
        self, latest_block: int
    ) -> tuple[list[tuple[str, BlockNumber]], list[tuple[str, BlockNumber]]]:
        """Returns (missed balances, missed prices) — list of (address, last_block)."""
        threshold_block = latest_block - int(MEMBER_INACTIVITY / BLOCK_TIME)

        dao = await rp.get_contract_by_name("rocketDAONodeTrusted")
        member_count = await dao.functions.getMemberCount().call()
        addresses = await rp.multicall(
            [dao.functions.getMemberAt(i) for i in range(member_count)]
        )
        addresses = [w3.to_checksum_address(addr) for addr in addresses]

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
        channel_id = cfg.discord.channels.get("odao")
        if not channel_id:
            return

        channel = await self.bot.get_or_fetch_channel(channel_id)
        assert isinstance(channel, Messageable)

        now = datetime.now(tz=UTC)
        for duty in DUTIES:
            last_update, period = await duty.fetch()
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
            embed = Embed(title=":warning: Missed oDAO Duty")
            embed.description = (
                f"The Oracle DAO has not performed the **{duty.name}** on time.\n\n"
                f"Last update: {discord_utils.format_dt(last_update, 'R')} "
                f"({discord_utils.format_dt(last_update, 'f')})\n"
                f"Expected every **{humanize.naturaldelta(period)}**, "
                f"due {discord_utils.format_dt(deadline, 'R')}."
            )
            await channel.send(embed=embed)

        latest_block = await w3.eth.get_block_number()
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

            embed = Embed(title=":warning: Inactive oDAO Members")
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
