import logging
from typing import Literal, NamedTuple, cast

from aiocache import cached
from discord import Interaction
from discord.app_commands import command, describe
from discord.ext.commands import Cog
from eth_typing import BlockIdentifier, ChecksumAddress

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.embeds import el_explorer_url
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.shared_w3 import w3
from rocketwatch.utils.views import PageView
from rocketwatch.utils.visibility import is_hidden

log = logging.getLogger("rocketwatch.queue")


class Queue(Cog):
    class Entry(NamedTuple):
        megapool: ChecksumAddress
        validator_id: int
        bond: int  # always 4,000 for now
        deposit_size: int  # always 32,000 for now

    def __init__(self, bot: RocketWatch):
        self.bot = bot

    class ValidatorPageView(PageView):
        def __init__(self, lane: Literal["combined", "standard", "express"]):
            super().__init__(page_size=15)
            if lane == "standard":
                self.queue_name = "🐢 Validator Standard Queue"
                self.content_loader = Queue.get_standard_queue
            elif lane == "express":
                self.queue_name = "🐇 Validator Express Queue"
                self.content_loader = Queue.get_express_queue
            else:
                self.queue_name = "Validator Queue"
                self.content_loader = Queue.get_combined_queue

        @property
        def _title(self) -> str:
            return self.queue_name

        async def _load_content(self, from_idx: int, to_idx: int) -> tuple[int, str]:
            queue_length, queue_content = await self.content_loader(
                limit=(to_idx - from_idx + 1), start=from_idx
            )
            return queue_length, queue_content

    @staticmethod
    @cached(key_builder=lambda _, address, prefix="": (address, prefix))
    async def _cached_el_url(address: ChecksumAddress, prefix: str = "") -> str:
        return str(
            await el_explorer_url(address, name_fmt=lambda n: f"`{n}`", prefix=prefix)
        )

    @staticmethod
    async def _megapool_to_node(megapool_address: ChecksumAddress) -> ChecksumAddress:
        return cast(
            ChecksumAddress,
            await rp.call(
                "rocketMegapoolDelegate.getNodeAddress", address=megapool_address
            ),
        )

    @staticmethod
    async def __format_queue_entry(entry: "Queue.Entry") -> str:
        node_address = await Queue._megapool_to_node(entry.megapool)
        node_label = await Queue._cached_el_url(node_address)
        return f"{node_label} #`{entry.validator_id}`"

    @staticmethod
    async def get_standard_queue(limit: int, start: int = 0) -> tuple[int, str]:
        """Get the next {limit} validators in the standard queue"""
        return await Queue._get_queue("deposit.queue.standard", limit, start)

    @staticmethod
    async def get_express_queue(limit: int, start: int = 0) -> tuple[int, str]:
        """Get the next {limit} validators in the express queue"""
        return await Queue._get_queue("deposit.queue.express", limit, start)

    @staticmethod
    async def _scan_list(
        namespace: bytes, start: int, limit: int, block_identifier: BlockIdentifier
    ) -> list["Queue.Entry"]:
        list_contract = await rp.get_contract_by_name("linkedListStorage")
        raw_entries, _ = await list_contract.functions.scan(
            namespace, 0, start + limit
        ).call(block_identifier=block_identifier)
        return [Queue.Entry(*entry) for entry in raw_entries][start:]

    @staticmethod
    async def _get_queue(namespace: str, limit: int, start: int = 0) -> tuple[int, str]:
        if limit <= 0:
            return 0, ""

        list_contract = await rp.get_contract_by_name("linkedListStorage")
        queue_namespace = bytes(w3.solidity_keccak(["string"], [namespace]))

        start = max(start, 0)
        latest_block = await w3.eth.get_block_number()
        q_len = await list_contract.functions.getLength(queue_namespace).call(
            block_identifier=latest_block
        )

        if start >= q_len:
            return q_len, ""

        queue_entries = await Queue._scan_list(
            queue_namespace, start, limit, latest_block
        )

        content = ""
        for i, entry in enumerate(queue_entries):
            entry_str = await Queue.__format_queue_entry(entry)
            content += f"{start + i + 1}. {entry_str}\n"

        return q_len, content

    @staticmethod
    def _get_entries_used_in_interval(
        start: int, end: int, len_express: int, len_standard: int, express_rate: int
    ) -> tuple[int, int]:
        log.debug(
            f"Calculating entries used in interval [{start}, {end}] with express_rate {express_rate}"
            f" and queue lengths {len_express} (express) and {len_standard} (standard)"
        )

        total_entries = end - start + 1  # end is inclusive
        num_standard = total_entries // (express_rate + 1)
        # standard queue is used when index % (express_queue_rate + 1) == express_queue_rate
        # this checks whether we "cross" an extra express queue slot in the interval
        if ((end + 1) % (express_rate + 1)) < (start % (express_rate + 1)):
            num_standard += 1

        num_standard = min(num_standard, len_standard)
        # if standard queue runs out, remaining entries are taken from express queue
        num_express = min(total_entries - num_standard, len_express)
        # if express queue runs out, remaining entries are taken from standard queue
        if (num_express + num_standard) < total_entries:
            num_standard = min(total_entries - num_express, len_standard)

        return num_express, num_standard

    @staticmethod
    async def get_combined_queue(limit: int, start: int = 0) -> tuple[int, str]:
        """Get the next {limit} validators in the combined queue (express + standard)"""

        latest_block = await w3.eth.get_block_number()
        express_queue_rate = await rp.call(
            "rocketDAOProtocolSettingsDeposit.getExpressQueueRate", block=latest_block
        )
        queue_index = await rp.call(
            "rocketDepositPool.getQueueIndex", block=latest_block
        )

        list_contract = await rp.get_contract_by_name("linkedListStorage")
        exp_namespace = bytes(w3.solidity_keccak(["string"], ["deposit.queue.express"]))
        std_namespace = bytes(
            w3.solidity_keccak(["string"], ["deposit.queue.standard"])
        )

        express_queue_length = await list_contract.functions.getLength(
            exp_namespace
        ).call(block_identifier=latest_block)
        standard_queue_length = await list_contract.functions.getLength(
            std_namespace
        ).call(block_identifier=latest_block)
        q_len = express_queue_length + standard_queue_length

        if start >= q_len:
            return q_len, ""

        start_express_queue, start_standard_queue = Queue._get_entries_used_in_interval(
            queue_index,
            queue_index + start - 1,
            express_queue_length,
            standard_queue_length,
            express_queue_rate,
        )
        log.debug(f"{start_express_queue = }")
        log.debug(f"{start_standard_queue = }")
        limit_express_queue, limit_standard_queue = Queue._get_entries_used_in_interval(
            queue_index + start,
            queue_index + start + limit - 1,
            express_queue_length - start_express_queue,
            standard_queue_length - start_standard_queue,
            express_queue_rate,
        )
        log.debug(f"{limit_express_queue = }")
        log.debug(f"{limit_standard_queue = }")

        express_entries_rev = (
            await Queue._scan_list(
                exp_namespace, start_express_queue, limit_express_queue, latest_block
            )
        )[::-1]
        standard_entries_rev = (
            await Queue._scan_list(
                std_namespace, start_standard_queue, limit_standard_queue, latest_block
            )
        )[::-1]

        content = ""
        for i in range(len(express_entries_rev) + len(standard_entries_rev)):
            effective_queue_index = queue_index + start + i
            is_express = (
                effective_queue_index % (express_queue_rate + 1)
            ) != express_queue_rate
            if (is_express and express_entries_rev) or (not standard_entries_rev):
                entry = express_entries_rev.pop()
                lane_pos = "🐇"
            else:
                entry = standard_entries_rev.pop()
                lane_pos = "🐢"

            overall_pos = start + i + 1
            entry_str = await Queue.__format_queue_entry(entry)
            content += f"{overall_pos}. {lane_pos} {entry_str}\n"

        return q_len, content

    @command()
    @describe(lane="type of queue to display")
    async def queue(
        self,
        interaction: Interaction,
        lane: Literal["combined", "standard", "express"] = "combined",
    ) -> None:
        """Show the RP validator queue"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        view = Queue.ValidatorPageView(lane)
        embed = await view.load()
        await interaction.followup.send(embed=embed, view=view)


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(Queue(bot))
