from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from google.genai import Client as GoogleClient
    from openai import AsyncOpenAI

    from rocketwatch.utils.config import LLMConfig

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ImageInput:
    data: bytes
    media_type: str


class LLMProvider(ABC):
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    @abstractmethod
    async def complete(
        self,
        system: str,
        user_message: str,
        *,
        max_tokens: int = 1024,
        images: list[ImageInput] | None = None,
    ) -> str: ...

    @abstractmethod
    async def complete_structured(
        self,
        system: str,
        user_message: str,
        schema: type[T],
        *,
        max_tokens: int = 1024,
        images: list[ImageInput] | None = None,
    ) -> T: ...


class AnthropicProvider(LLMProvider):
    def _get_client(self) -> AsyncAnthropic:
        from anthropic import AsyncAnthropic

        if not hasattr(self, "_client"):
            self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    @staticmethod
    def _build_user_content(user_message: str, images: list[ImageInput] | None) -> Any:
        if not images:
            return user_message
        blocks: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.media_type,
                    "data": base64.b64encode(img.data).decode("ascii"),
                },
            }
            for img in images
        ]
        blocks.append({"type": "text", "text": user_message})
        return blocks

    async def complete(
        self,
        system: str,
        user_message: str,
        *,
        max_tokens: int = 1024,
        images: list[ImageInput] | None = None,
    ) -> str:
        from anthropic.types import TextBlock

        response = await self._get_client().messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": self._build_user_content(user_message, images),
                }
            ],
        )
        block = response.content[0]
        assert isinstance(block, TextBlock)
        return block.text

    async def complete_structured(
        self,
        system: str,
        user_message: str,
        schema: type[T],
        *,
        max_tokens: int = 1024,
        images: list[ImageInput] | None = None,
    ) -> T:
        from anthropic.types import ToolUseBlock

        tool_name = schema.__name__
        response = await self._get_client().messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": self._build_user_content(user_message, images),
                }
            ],
            tools=[
                {
                    "name": tool_name,
                    "description": f"Return the result as a {tool_name} object.",
                    "input_schema": schema.model_json_schema(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
        )
        block = next(b for b in response.content if isinstance(b, ToolUseBlock))
        return schema.model_validate(block.input)


class OpenAIProvider(LLMProvider):
    def _get_client(self) -> AsyncOpenAI:
        from openai import AsyncOpenAI

        if not hasattr(self, "_client"):
            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    @staticmethod
    def _build_user_content(user_message: str, images: list[ImageInput] | None) -> Any:
        if not images:
            return user_message
        parts: list[dict[str, Any]] = [
            {
                "type": "image_url",
                "image_url": {
                    "url": (
                        f"data:{img.media_type};base64,"
                        + base64.b64encode(img.data).decode("ascii")
                    )
                },
            }
            for img in images
        ]
        parts.append({"type": "text", "text": user_message})
        return parts

    async def complete(
        self,
        system: str,
        user_message: str,
        *,
        max_tokens: int = 1024,
        images: list[ImageInput] | None = None,
    ) -> str:
        response = await self._get_client().chat.completions.create(
            model=self._model,
            max_completion_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": self._build_user_content(user_message, images),
                },
            ],
        )
        content = response.choices[0].message.content
        assert isinstance(content, str)
        return content

    async def complete_structured(
        self,
        system: str,
        user_message: str,
        schema: type[T],
        *,
        max_tokens: int = 1024,
        images: list[ImageInput] | None = None,
    ) -> T:
        response = await self._get_client().beta.chat.completions.parse(
            model=self._model,
            max_completion_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": self._build_user_content(user_message, images),
                },
            ],
            response_format=schema,
        )
        parsed = response.choices[0].message.parsed
        assert isinstance(parsed, schema)
        return parsed


class GoogleProvider(LLMProvider):
    def _get_client(self) -> GoogleClient:
        from google import genai

        if not hasattr(self, "_client"):
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    @staticmethod
    def _build_contents(
        user_message: str, images: list[ImageInput] | None
    ) -> list[Any]:
        from google.genai import types

        parts: list[Any] = [
            types.Part.from_bytes(data=img.data, mime_type=img.media_type)
            for img in (images or [])
        ]
        parts.append(types.Part.from_text(text=user_message))
        return parts

    async def complete(
        self,
        system: str,
        user_message: str,
        *,
        max_tokens: int = 1024,
        images: list[ImageInput] | None = None,
    ) -> str:
        from google import genai

        response = await self._get_client().aio.models.generate_content(
            model=self._model,
            contents=self._build_contents(user_message, images),
            config=genai.types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
            ),
        )
        text = response.text
        assert isinstance(text, str)
        return text

    async def complete_structured(
        self,
        system: str,
        user_message: str,
        schema: type[T],
        *,
        max_tokens: int = 1024,
        images: list[ImageInput] | None = None,
    ) -> T:
        from google import genai

        response = await self._get_client().aio.models.generate_content(
            model=self._model,
            contents=self._build_contents(user_message, images),
            config=genai.types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        text = response.text
        assert isinstance(text, str)
        return schema.model_validate_json(text)


_PROVIDERS: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "google": GoogleProvider,
}


def create_provider(config: LLMConfig) -> LLMProvider | None:
    """Create an LLM provider from config. Returns None if not configured."""
    if not config.provider or not config.api_key:
        return None
    cls = _PROVIDERS[config.provider]
    return cls(api_key=config.api_key, model=config.model)
