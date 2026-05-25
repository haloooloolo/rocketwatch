import base64
import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from rocketwatch.utils.config import LLMConfig
from rocketwatch.utils.llm import (
    AnthropicProvider,
    GoogleProvider,
    ImageInput,
    OpenAIProvider,
    create_provider,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_BYTES = b"\xff\xd8\xff" + b"\x00" * 16


class _Verdict(BaseModel):
    verdict: str


# ---- create_provider ------------------------------------------------------


class TestCreateProvider:
    def test_unconfigured_returns_none(self) -> None:
        # An unset provider (default-constructed LLMConfig) should not raise —
        # it just means the feature is disabled.
        assert create_provider(LLMConfig()) is None

    def test_missing_api_key_returns_none(self) -> None:
        # Provider name without a key is also "not configured".
        cfg = LLMConfig(provider="anthropic", api_key="", model="claude-x")
        assert create_provider(cfg) is None

    def test_missing_provider_returns_none(self) -> None:
        cfg = LLMConfig(provider="", api_key="key", model="m")
        assert create_provider(cfg) is None

    def test_returns_anthropic_provider(self) -> None:
        cfg = LLMConfig(provider="anthropic", api_key="key", model="m")
        provider = create_provider(cfg)
        assert isinstance(provider, AnthropicProvider)

    def test_returns_openai_provider(self) -> None:
        cfg = LLMConfig(provider="openai", api_key="key", model="m")
        provider = create_provider(cfg)
        assert isinstance(provider, OpenAIProvider)

    def test_returns_google_provider(self) -> None:
        cfg = LLMConfig(provider="google", api_key="key", model="m")
        provider = create_provider(cfg)
        assert isinstance(provider, GoogleProvider)

    def test_constructed_provider_remembers_model(self) -> None:
        # The model/api_key passed in via config must reach the provider
        # so per-call requests use the right model.
        cfg = LLMConfig(provider="anthropic", api_key="k", model="claude-x")
        provider = create_provider(cfg)
        assert provider is not None
        assert provider._model == "claude-x"
        assert provider._api_key == "k"


# ---- Anthropic content shape ----------------------------------------------


class TestAnthropicBuildUserContent:
    def test_no_images_returns_plain_string(self) -> None:
        # When there's nothing to attach, the user message stays as-is —
        # avoids the block array overhead for simple prompts.
        assert AnthropicProvider._build_user_content("hello", None) == "hello"

    def test_empty_images_list_returns_plain_string(self) -> None:
        # Empty list is "no images", same as None.
        assert AnthropicProvider._build_user_content("hello", []) == "hello"

    def test_with_images_returns_block_list(self) -> None:
        result = AnthropicProvider._build_user_content(
            "describe this", [ImageInput(data=PNG_BYTES, media_type="image/png")]
        )
        assert isinstance(result, list)
        # Spec: image blocks come before the text block so the model has the
        # image in context when reading the prompt.
        assert result[0]["type"] == "image"
        assert result[-1]["type"] == "text"
        assert result[-1]["text"] == "describe this"

    def test_image_payload_is_base64(self) -> None:
        result = AnthropicProvider._build_user_content(
            "x", [ImageInput(data=PNG_BYTES, media_type="image/png")]
        )
        encoded = result[0]["source"]["data"]
        # The raw bytes should round-trip through base64.
        assert base64.b64decode(encoded) == PNG_BYTES

    def test_image_media_type_preserved(self) -> None:
        result = AnthropicProvider._build_user_content(
            "x", [ImageInput(data=JPEG_BYTES, media_type="image/jpeg")]
        )
        assert result[0]["source"]["media_type"] == "image/jpeg"

    def test_multiple_images_all_included(self) -> None:
        images = [
            ImageInput(data=PNG_BYTES, media_type="image/png"),
            ImageInput(data=JPEG_BYTES, media_type="image/jpeg"),
        ]
        result = AnthropicProvider._build_user_content("two pics", images)
        # Two image blocks + one text block.
        image_blocks = [b for b in result if b["type"] == "image"]
        assert len(image_blocks) == 2


# ---- OpenAI content shape -------------------------------------------------


class TestOpenAIBuildUserContent:
    def test_no_images_returns_plain_string(self) -> None:
        assert OpenAIProvider._build_user_content("hi", None) == "hi"

    def test_empty_images_list_returns_plain_string(self) -> None:
        assert OpenAIProvider._build_user_content("hi", []) == "hi"

    def test_with_images_returns_part_list_with_data_url(self) -> None:
        result = OpenAIProvider._build_user_content(
            "what's this", [ImageInput(data=PNG_BYTES, media_type="image/png")]
        )
        # OpenAI's API wants data URLs, not raw base64 + media_type fields.
        assert isinstance(result, list)
        assert result[0]["type"] == "image_url"
        url = result[0]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        b64_part = url.split(",", 1)[1]
        assert base64.b64decode(b64_part) == PNG_BYTES

    def test_text_part_follows_images(self) -> None:
        # Same ordering spec as Anthropic: image first, then the prompt.
        result = OpenAIProvider._build_user_content(
            "describe", [ImageInput(data=PNG_BYTES, media_type="image/png")]
        )
        assert result[-1] == {"type": "text", "text": "describe"}


# ---- ImageInput contract --------------------------------------------------


class TestImageInput:
    def test_is_immutable(self) -> None:
        # Frozen dataclass — guards against accidental mutation between
        # provider implementations swapping fields.
        img = ImageInput(data=b"abc", media_type="image/png")
        with pytest.raises(dataclasses.FrozenInstanceError):
            img.data = b"def"  # type: ignore[misc]


# ---- Provider request/response round-trips --------------------------------
# Each provider lazily constructs an SDK client and shapes the request; we mock
# the client so the test exercises our request-building + response-parsing.


class TestAnthropicProvider:
    def test_get_client_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import anthropic

        instance = MagicMock()
        ctor = MagicMock(return_value=instance)
        monkeypatch.setattr(anthropic, "AsyncAnthropic", ctor)
        provider = AnthropicProvider("key", "m")
        assert provider._get_client() is instance
        assert provider._get_client() is instance
        ctor.assert_called_once()

    async def test_complete_returns_block_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from anthropic.types import TextBlock

        block = MagicMock(spec=TextBlock)
        block.text = "hello"
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=MagicMock(content=[block]))
        provider = AnthropicProvider("k", "m")
        monkeypatch.setattr(provider, "_get_client", lambda: client)

        assert await provider.complete("sys", "msg") == "hello"

    async def test_complete_structured_parses_tool_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from anthropic.types import ToolUseBlock

        block = MagicMock(spec=ToolUseBlock)
        block.input = {"verdict": "scam"}
        client = MagicMock()
        client.messages.create = AsyncMock(return_value=MagicMock(content=[block]))
        provider = AnthropicProvider("k", "m")
        monkeypatch.setattr(provider, "_get_client", lambda: client)

        result = await provider.complete_structured("sys", "msg", _Verdict)
        assert result == _Verdict(verdict="scam")


class TestOpenAIProvider:
    def test_get_client_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import openai

        instance = MagicMock()
        ctor = MagicMock(return_value=instance)
        monkeypatch.setattr(openai, "AsyncOpenAI", ctor)
        provider = OpenAIProvider("key", "m")
        assert provider._get_client() is instance
        assert provider._get_client() is instance
        ctor.assert_called_once()

    async def test_complete_returns_message_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = MagicMock(choices=[MagicMock(message=MagicMock(content="answer"))])
        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=response)
        provider = OpenAIProvider("k", "m")
        monkeypatch.setattr(provider, "_get_client", lambda: client)

        assert await provider.complete("sys", "msg") == "answer"

    async def test_complete_structured_returns_parsed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed = _Verdict(verdict="ok")
        response = MagicMock(choices=[MagicMock(message=MagicMock(parsed=parsed))])
        client = MagicMock()
        client.beta.chat.completions.parse = AsyncMock(return_value=response)
        provider = OpenAIProvider("k", "m")
        monkeypatch.setattr(provider, "_get_client", lambda: client)

        assert await provider.complete_structured("sys", "msg", _Verdict) is parsed


class TestGoogleProvider:
    def test_get_client_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from google import genai

        instance = MagicMock()
        ctor = MagicMock(return_value=instance)
        monkeypatch.setattr(genai, "Client", ctor)
        provider = GoogleProvider("key", "m")
        assert provider._get_client() is instance
        assert provider._get_client() is instance
        ctor.assert_called_once()

    def test_build_contents_includes_image_and_text(self) -> None:
        parts = GoogleProvider._build_contents(
            "hi", [ImageInput(data=PNG_BYTES, media_type="image/png")]
        )
        assert len(parts) == 2

    def test_build_contents_text_only(self) -> None:
        assert len(GoogleProvider._build_contents("hi", None)) == 1

    async def test_complete_returns_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = MagicMock()
        client.aio.models.generate_content = AsyncMock(
            return_value=MagicMock(text="answer")
        )
        provider = GoogleProvider("k", "m")
        monkeypatch.setattr(provider, "_get_client", lambda: client)

        assert await provider.complete("sys", "msg") == "answer"

    async def test_complete_structured_parses_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.aio.models.generate_content = AsyncMock(
            return_value=MagicMock(text='{"verdict": "scam"}')
        )
        provider = GoogleProvider("k", "m")
        monkeypatch.setattr(provider, "_get_client", lambda: client)

        result = await provider.complete_structured("sys", "msg", _Verdict)
        assert result == _Verdict(verdict="scam")
