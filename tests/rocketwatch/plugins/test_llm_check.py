from typing import Any
from unittest.mock import AsyncMock, MagicMock

from rocketwatch.plugins.scam_detection import llm_check as llm
from rocketwatch.plugins.scam_detection.llm_check import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    LLMScamChecker,
    ScamCheckResult,
)


def _attachment(content_type: str, size: int) -> MagicMock:
    a = MagicMock()
    a.content_type = content_type
    a.size = size
    a.url = "http://a"
    a.read = AsyncMock(return_value=b"data")
    return a


def _message(**over: Any) -> MagicMock:
    m = MagicMock()
    m.content = over.get("content", "hi")
    m.embeds = []
    m.attachments = over.get("attachments", [])
    m.message_snapshots = []
    return m


def _checker(provider: Any) -> LLMScamChecker:
    checker = LLMScamChecker.__new__(LLMScamChecker)
    checker._provider = provider
    checker.enabled = provider is not None
    return checker


class TestFetchImageAttachments:
    async def test_filters_non_image_and_oversized(self) -> None:
        message = _message(
            attachments=[
                _attachment("image/png", 1000),
                _attachment("text/plain", 1000),
                _attachment("image/jpeg", MAX_IMAGE_BYTES + 1),
            ]
        )
        images = await llm._fetch_image_attachments(message)
        assert len(images) == 1
        assert images[0].media_type == "image/png"

    async def test_caps_at_max_images(self) -> None:
        message = _message(
            attachments=[_attachment("image/png", 100) for _ in range(MAX_IMAGES + 2)]
        )
        images = await llm._fetch_image_attachments(message)
        assert len(images) == MAX_IMAGES

    async def test_read_failure_is_skipped(self) -> None:
        bad = _attachment("image/png", 100)
        bad.read = AsyncMock(side_effect=RuntimeError("io"))
        images = await llm._fetch_image_attachments(_message(attachments=[bad]))
        assert images == []


class TestLLMScamCheck:
    async def test_returns_reason_when_flagged(self) -> None:
        provider = MagicMock()
        provider.complete_structured = AsyncMock(
            return_value=ScamCheckResult(is_scam=True, reason="DM lure")
        )
        result = await _checker(provider).check(_message(), user_msg_count=1)
        assert result == "DM lure"

    async def test_returns_unknown_when_flagged_without_reason(self) -> None:
        provider = MagicMock()
        provider.complete_structured = AsyncMock(
            return_value=ScamCheckResult(is_scam=True, reason="")
        )
        result = await _checker(provider).check(_message(), user_msg_count=1)
        assert result == "Unknown"

    async def test_returns_none_when_not_scam(self) -> None:
        provider = MagicMock()
        provider.complete_structured = AsyncMock(
            return_value=ScamCheckResult(is_scam=False, reason="benign")
        )
        result = await _checker(provider).check(_message(), user_msg_count=1)
        assert result is None

    async def test_disabled_provider_returns_none(self) -> None:
        result = await _checker(None).check(_message(), user_msg_count=1)
        assert result is None
