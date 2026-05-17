import base64
import dataclasses

import pytest

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


# ---- create_provider ------------------------------------------------------


class TestCreateProvider:
    def test_unconfigured_returns_none(self):
        # An unset provider (default-constructed LLMConfig) should not raise —
        # it just means the feature is disabled.
        assert create_provider(LLMConfig()) is None

    def test_missing_api_key_returns_none(self):
        # Provider name without a key is also "not configured".
        cfg = LLMConfig(provider="anthropic", api_key="", model="claude-x")
        assert create_provider(cfg) is None

    def test_missing_provider_returns_none(self):
        cfg = LLMConfig(provider="", api_key="key", model="m")
        assert create_provider(cfg) is None

    def test_returns_anthropic_provider(self):
        cfg = LLMConfig(provider="anthropic", api_key="key", model="m")
        provider = create_provider(cfg)
        assert isinstance(provider, AnthropicProvider)

    def test_returns_openai_provider(self):
        cfg = LLMConfig(provider="openai", api_key="key", model="m")
        provider = create_provider(cfg)
        assert isinstance(provider, OpenAIProvider)

    def test_returns_google_provider(self):
        cfg = LLMConfig(provider="google", api_key="key", model="m")
        provider = create_provider(cfg)
        assert isinstance(provider, GoogleProvider)

    def test_constructed_provider_remembers_model(self):
        # The model/api_key passed in via config must reach the provider
        # so per-call requests use the right model.
        cfg = LLMConfig(provider="anthropic", api_key="k", model="claude-x")
        provider = create_provider(cfg)
        assert provider is not None
        assert provider._model == "claude-x"
        assert provider._api_key == "k"


# ---- Anthropic content shape ----------------------------------------------


class TestAnthropicBuildUserContent:
    def test_no_images_returns_plain_string(self):
        # When there's nothing to attach, the user message stays as-is —
        # avoids the block array overhead for simple prompts.
        assert AnthropicProvider._build_user_content("hello", None) == "hello"

    def test_empty_images_list_returns_plain_string(self):
        # Empty list is "no images", same as None.
        assert AnthropicProvider._build_user_content("hello", []) == "hello"

    def test_with_images_returns_block_list(self):
        result = AnthropicProvider._build_user_content(
            "describe this", [ImageInput(data=PNG_BYTES, media_type="image/png")]
        )
        assert isinstance(result, list)
        # Spec: image blocks come before the text block so the model has the
        # image in context when reading the prompt.
        assert result[0]["type"] == "image"
        assert result[-1]["type"] == "text"
        assert result[-1]["text"] == "describe this"

    def test_image_payload_is_base64(self):
        result = AnthropicProvider._build_user_content(
            "x", [ImageInput(data=PNG_BYTES, media_type="image/png")]
        )
        encoded = result[0]["source"]["data"]
        # The raw bytes should round-trip through base64.
        assert base64.b64decode(encoded) == PNG_BYTES

    def test_image_media_type_preserved(self):
        result = AnthropicProvider._build_user_content(
            "x", [ImageInput(data=JPEG_BYTES, media_type="image/jpeg")]
        )
        assert result[0]["source"]["media_type"] == "image/jpeg"

    def test_multiple_images_all_included(self):
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
    def test_no_images_returns_plain_string(self):
        assert OpenAIProvider._build_user_content("hi", None) == "hi"

    def test_empty_images_list_returns_plain_string(self):
        assert OpenAIProvider._build_user_content("hi", []) == "hi"

    def test_with_images_returns_part_list_with_data_url(self):
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

    def test_text_part_follows_images(self):
        # Same ordering spec as Anthropic: image first, then the prompt.
        result = OpenAIProvider._build_user_content(
            "describe", [ImageInput(data=PNG_BYTES, media_type="image/png")]
        )
        assert result[-1] == {"type": "text", "text": "describe"}


# ---- ImageInput contract --------------------------------------------------


class TestImageInput:
    def test_is_immutable(self):
        # Frozen dataclass — guards against accidental mutation between
        # provider implementations swapping fields.
        img = ImageInput(data=b"abc", media_type="image/png")
        with pytest.raises(dataclasses.FrozenInstanceError):
            img.data = b"def"  # type: ignore[misc]
