import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from discord import Thread

from utils.config import Config, cfg


def _get_test_cfg():
    from utils.config import (
        ConsensusLayerConfig,
        DiscordConfig,
        DiscordOwner,
        DmWarningConfig,
        EventsConfig,
        ExecutionLayerConfig,
        ExecutionLayerEndpoint,
        MongoDBConfig,
        RocketPoolConfig,
        RocketPoolSupport,
    )

    return Config(
        discord=DiscordConfig(
            secret="test",
            owner=DiscordOwner(user_id=1, server_id=2),
            channels={"default": 100, "report_scams": 200},
        ),
        execution_layer=ExecutionLayerConfig(
            explorer="https://etherscan.io",
            endpoint=ExecutionLayerEndpoint(
                current="http://localhost:8545", mainnet="http://localhost:8545"
            ),
        ),
        consensus_layer=ConsensusLayerConfig(
            explorer="https://beaconcha.in",
            endpoint="http://localhost:5052",
            beaconcha_secret="test",
        ),
        mongodb=MongoDBConfig(uri="mongodb://localhost:27017"),
        rocketpool=RocketPoolConfig(
            manual_addresses={"rocketStorage": "0x1234"},
            dao_multisigs=["0xabcd"],
            support=RocketPoolSupport(
                user_ids=[1], role_ids=[2], server_id=3, channel_id=4, moderator_id=5
            ),
            dm_warning=DmWarningConfig(channels=[100]),
        ),
        events=EventsConfig(lookback_distance=100, genesis=0, block_batch_size=50),
    )


def _load_test_cases():
    path = Path(__file__).parent / "message_samples.json"
    with open(path) as f:
        return json.load(f)


TEST_CASES = _load_test_cases()


def _make_embed(data: dict) -> MagicMock:
    embed = MagicMock()
    embed.title = data.get("title")
    embed.description = data.get("description")
    return embed


def _make_message(case: dict) -> MagicMock:
    msg = MagicMock()
    msg.content = case["content"]
    msg.embeds = [_make_embed(e) for e in case.get("embeds", [])]
    msg.author.guild_permissions.mention_everyone = False
    return msg


def _make_checks():
    cfg._instance = _get_test_cfg()
    from plugins.scam_detection.checks import ScamChecks

    return ScamChecks()


def _make_detector():
    cfg._instance = _get_test_cfg()
    bot = MagicMock()
    bot.tree = MagicMock()
    with patch.object(bot.tree, "add_command"):
        from plugins.scam_detection.scam_detection import ScamDetection

        return ScamDetection(bot)


@pytest.fixture(scope="module")
def checks():
    return _make_checks()


def _check_message(checks, case: dict) -> list[str]:
    msg = _make_message(case)
    results = [
        checks._obfuscated_url,
        checks._ticket_system,
        checks._suspicious_x_account,
        checks._suspicious_link,
        checks._discord_invite,
        checks._tap_on_this,
        checks._spam_wall,
    ]
    return [r for check in results if (r := check(msg))]


def _case_id(case):
    return case["content"][:100]


class TestMessageDetection:
    @pytest.mark.parametrize("case", TEST_CASES["messages"]["unsafe"], ids=_case_id)
    def test_unsafe_message_detected(self, checks, case):
        reasons = _check_message(checks, case)
        assert reasons, f"Unsafe message not detected: {case['content'][:100]!r}"

    @pytest.mark.parametrize("case", TEST_CASES["messages"]["safe"], ids=_case_id)
    def test_safe_message_not_flagged(self, checks, case):
        reasons = _check_message(checks, case)
        assert not reasons, f"Safe message falsely flagged: {reasons}"


class TestThreadStarterDeleted:
    @pytest.fixture()
    def detector(self):
        return _make_detector()

    def _make_thread(self, thread_id, owner_id, guild_id):
        thread = MagicMock(spec=Thread)
        thread.id = thread_id
        thread.owner_id = owner_id
        thread.guild.id = guild_id
        thread.guild.get_member.return_value = MagicMock(
            bot=False,
            guild_permissions=MagicMock(moderate_members=False),
            roles=[],
            id=owner_id,
        )
        return thread

    @pytest.mark.asyncio
    async def test_on_thread_create_tracks_thread(self, detector):
        thread = self._make_thread(123, 999, cfg.rocketpool.support.server_id)
        await detector.on_thread_create(thread)
        assert 123 in detector._thread_creation_messages

    @pytest.mark.asyncio
    async def test_on_thread_create_ignores_other_guilds(self, detector):
        thread = self._make_thread(123, 999, 0)
        await detector.on_thread_create(thread)
        assert 123 not in detector._thread_creation_messages

    @pytest.mark.asyncio
    async def test_starter_deleted_reports_thread(self, detector):
        thread_id = 123
        thread = self._make_thread(thread_id, 999, cfg.rocketpool.support.server_id)
        detector._thread_creation_messages[thread_id] = thread_id
        detector.bot.get_or_fetch_channel = AsyncMock(return_value=thread)
        detector.report_thread = AsyncMock()

        await detector._check_thread_starter_deleted(thread_id)

        detector.report_thread.assert_awaited_once_with(
            thread, "Attempt to hide thread from main channel"
        )
        assert thread_id not in detector._thread_creation_messages

    @pytest.mark.asyncio
    async def test_starter_deleted_ignores_untracked(self, detector):
        detector.report_thread = AsyncMock()
        await detector._check_thread_starter_deleted(456)
        detector.report_thread.assert_not_awaited()
