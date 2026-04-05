import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

# Add sentinel source to path for bare imports (config, guardrails, etc.)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "sentinel"))
# Add this test package to path so helpers.py is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import make_mock_bot, make_mock_guild, make_test_config

from sentinel.config import cfg
from sentinel.guardrails import rate_limiter
from sentinel.server import create_app


@pytest.fixture(autouse=True)
def _inject_test_config():
    cfg._instance = make_test_config()
    yield
    cfg._instance = None


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limiter._timestamps.clear()
    yield
    rate_limiter._timestamps.clear()


@pytest.fixture
async def client():
    guild = make_mock_guild()
    bot = make_mock_bot(guild)
    app = create_app(bot)
    async with TestClient(TestServer(app)) as c:
        c.mock_bot = bot
        c.mock_guild = guild
        yield c
