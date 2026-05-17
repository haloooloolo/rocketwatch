from __future__ import annotations

import importlib
from typing import Any

import pytest

from tests.support.discord_harness import (
    captured_embed,
    make_bot,
    make_interaction,
    run_command,
)


@pytest.fixture
def eight_ball_cog() -> Any:
    module = importlib.import_module("rocketwatch.plugins.8ball.8ball")
    return module.EightBall(make_bot())


async def test_question_with_question_mark_returns_an_oracle_answer(
    monkeypatch: pytest.MonkeyPatch, eight_ball_cog: Any
) -> None:
    monkeypatch.setattr(
        "rocketwatch.plugins.8ball.8ball.asyncio.sleep",
        lambda _seconds: _noop(),
    )

    interaction = make_interaction(user_name="alice")
    embed = await run_command(
        eight_ball_cog, "eight_ball", interaction, "Will Rocket Pool succeed?"
    )

    assert embed.title is not None and "8 Ball" in embed.title
    assert embed.description is not None
    assert "Will Rocket Pool succeed?" in embed.description
    assert "alice" in embed.description


async def test_question_missing_question_mark_returns_error_embed(
    eight_ball_cog: Any,
) -> None:
    interaction = make_interaction()
    await run_command(eight_ball_cog, "eight_ball", interaction, "no punctuation")

    embed = captured_embed(interaction)
    assert embed.description is not None
    assert "yes or no question" in embed.description

    # The error variant uses send_message directly, not the defer+followup path.
    interaction.response.send_message.assert_called_once()
    interaction.followup.send.assert_not_called()


async def _noop() -> None:
    return None
