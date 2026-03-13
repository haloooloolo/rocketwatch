import logging

from discord import Intents

from rocketwatch import RocketWatch
from utils.config import cfg

logging.basicConfig(
    format="%(levelname)5s %(asctime)s [%(name)s] %(filename)s:%(lineno)d|%(funcName)s(): %(message)s"
)
logging.getLogger().setLevel("INFO")
logging.getLogger("rocketwatch").setLevel(cfg.log_level)

log = logging.getLogger("rocketwatch.main")


def main() -> None:
    intents = Intents.none()
    intents.guilds = True
    intents.members = True
    intents.messages = True
    intents.message_content = True
    intents.reactions = True
    intents.moderation = True

    log.info("Starting bot...")
    bot = RocketWatch(intents=intents)
    bot.run(cfg.discord.secret)


if __name__ == "__main__":
    main()
