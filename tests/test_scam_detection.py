import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import regex as re

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
            endpoint=ExecutionLayerEndpoint(current="http://localhost:8545", mainnet="http://localhost:8545"),
            etherscan_secret="test",
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
            support=RocketPoolSupport(user_ids=[1], role_ids=[2], server_id=3, channel_id=4, moderator_id=5),
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


def _make_detector():
    cfg._instance = _get_test_cfg()
    bot = MagicMock()
    bot.tree = MagicMock()
    with patch.object(bot.tree, "add_command"):
        from plugins.scam_detection.scam_detection import DetectScam
        return DetectScam(bot)


@pytest.fixture(scope="module")
def detector():
    return _make_detector()


def _check_message(detector, case: dict) -> list[str]:
    msg = _make_message(case)
    checks = [
        detector._obfuscated_url,
        detector._ticket_system,
        detector._suspicious_x_account,
        detector._suspicious_link,
        detector._discord_invite,
        detector._tap_on_this,
        detector._bio_redirect,
        detector._spam_wall,
    ]
    return [r for check in checks if (r := check(msg))]


def _case_id(case):
    return case["content"][:100]


THREAD_KEYWORDS = ("support", "tick", "assistance", "error", "\U0001f3ab", "\U0001f39f\ufe0f")
THREAD_NAMES = (".", "!", "///")
THREAD_PATTERN = re.compile(r"(-|\u2013|\u2014)\d{3,}")


def _check_thread(name: str) -> bool:
    lower = name.strip().lower()
    return (
        any(kw in lower for kw in THREAD_KEYWORDS)
        or bool(THREAD_PATTERN.search(name))
        or lower in THREAD_NAMES
    )


class TestMessageDetection:
    @pytest.mark.parametrize("case", TEST_CASES["messages"]["unsafe"], ids=_case_id)
    def test_unsafe_message_detected(self, detector, case):
        reasons = _check_message(detector, case)
        assert reasons, f"Unsafe message not detected: {case['content'][:100]!r}"

    @pytest.mark.parametrize("case", TEST_CASES["messages"]["safe"], ids=_case_id)
    def test_safe_message_not_flagged(self, detector, case):
        reasons = _check_message(detector, case)
        assert not reasons, f"Safe message falsely flagged: {reasons}"

    @pytest.mark.parametrize("case", TEST_CASES["messages"]["known_false_positives"], ids=_case_id)
    @pytest.mark.xfail(reason="known false positive", strict=True)
    def test_known_false_positive(self, detector, case):
        reasons = _check_message(detector, case)
        assert not reasons, f"Falsely flagged: {reasons}"

    @pytest.mark.parametrize("case", TEST_CASES["messages"]["known_false_negatives"], ids=_case_id)
    @pytest.mark.xfail(reason="known false negative", strict=True)
    def test_known_false_negative(self, detector, case):
        reasons = _check_message(detector, case)
        assert reasons, f"Scam not detected: {case['content'][:100]!r}"


class TestThreadDetection:
    @pytest.mark.parametrize("name", TEST_CASES["threads"]["unsafe"])
    def test_unsafe_thread_detected(self, name):
        assert _check_thread(name), f"Unsafe thread name not detected: {name!r}"

    @pytest.mark.parametrize("name", TEST_CASES["threads"]["safe"])
    def test_safe_thread_not_flagged(self, name):
        assert not _check_thread(name), f"Safe thread name falsely flagged: {name!r}"

    @pytest.mark.parametrize("name", TEST_CASES["threads"]["known_false_positives"])
    @pytest.mark.xfail(reason="known false positive", strict=True)
    def test_known_false_positive(self, name):
        assert not _check_thread(name), f"Falsely flagged: {name!r}"
