import logging

import aiohttp

from utils.config import cfg
from utils.shared_w3 import w3

log = logging.getLogger("rocketwatch.etherscan")


async def get_recent_account_transactions(address, block_count=44800):
    ETHERSCAN_URL = "https://api.etherscan.io/api"

    highest_block = (await w3.eth.get_block("latest"))["number"]
    page = 1
    lowest_block = highest_block - block_count

    async with aiohttp.ClientSession() as session:
        resp = await session.get(ETHERSCAN_URL, params={"address"   : address,
                                                        "page"      : page,
                                                        "apikey"    : cfg.execution_layer.etherscan_secret,
                                                        "module"    : "account",
                                                        "action"    : "txlist",
                                                        "sort"      : "desc",
                                                        "startblock": lowest_block,
                                                        "endblock"  : highest_block})

        if resp.status != 200:
            log.debug(
                f"Error querying etherscan, unexpected HTTP {resp.status!s}")
            return

        parsed = await resp.json()
        if "message" not in parsed or parsed["message"].lower() != "ok":
            error = parsed.get("message", "")
            r = parsed.get("result", "")
            log.debug(f"Error querying {resp.url} - {error} - {r}")
            return

        def valid_tx(tx):
            if tx["to"] != address.lower():
                return False
            return int(tx["isError"]) == 0

        return {result["hash"]: result for result in parsed["result"] if valid_tx(result)}
