import asyncio
import logging

from aiohttp import web

from bot import SentinelBot
from config import cfg
from server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("sentinel")


async def main() -> None:
    bot = SentinelBot()
    app = create_app(bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.api.host, cfg.api.port)
    await site.start()
    log.info(f"API server listening on {cfg.api.host}:{cfg.api.port}")

    try:
        await bot.start(cfg.discord.token)
    finally:
        await runner.cleanup()


asyncio.run(main())
