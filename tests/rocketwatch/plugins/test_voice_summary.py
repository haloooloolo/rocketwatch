import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ext.voice_recv import VoiceRecvClient

from rocketwatch.plugins.voice_summary.session import CallSession
from rocketwatch.plugins.voice_summary.voice_summary import VoiceSummary
from rocketwatch.utils.config import LLMConfig, STTConfig, TranscriptionConfig, cfg
from tests.lib.discord_harness import make_bot

GRACE = 0.05


@pytest.fixture
def cog(monkeypatch: pytest.MonkeyPatch) -> VoiceSummary:
    assert cfg._instance is not None
    transcription = TranscriptionConfig(
        stt=STTConfig(provider="openai", model="gpt-transcribe"),
        llm=LLMConfig(provider="openai", api_key="k", model="m"),
        min_users=5,
    )
    # the field is whole seconds; assignment skips validation
    transcription.leave_grace_seconds = GRACE  # type: ignore[assignment]
    monkeypatch.setattr(
        cfg,
        "_instance",
        cfg._instance.model_copy(update={"transcription": transcription}),
    )
    return VoiceSummary(make_bot())


def _channel(humans: int) -> MagicMock:
    channel = MagicMock()
    channel.members = [MagicMock(bot=False) for _ in range(humans)]
    return channel


def _voice_client(channel: MagicMock, *, connected: bool = True) -> MagicMock:
    vc = MagicMock(spec=VoiceRecvClient)
    vc.channel = channel
    vc.is_connected.return_value = connected
    vc.disconnect = AsyncMock()
    return vc


def _recording(cog: VoiceSummary, channel: MagicMock, **vc_kwargs: Any) -> MagicMock:
    session = MagicMock()
    session.voice_client = _voice_client(channel, **vc_kwargs)
    session.finalize = AsyncMock(return_value=None)
    session.resume = AsyncMock()
    cog._session = session
    return session


def _member() -> MagicMock:
    member = MagicMock(bot=False)
    member.guild.id = cfg.rocketpool.support.server_id
    return member


async def _update(
    cog: VoiceSummary, member: MagicMock, before: Any, after: Any
) -> None:
    await cog.on_voice_state_update(
        member, MagicMock(channel=before), MagicMock(channel=after)
    )


class TestAutoStop:
    async def test_member_leaving_a_large_call_keeps_recording(
        self, cog: VoiceSummary
    ) -> None:
        channel = _channel(8)  # 9 before one left
        session = _recording(cog, channel)

        await _update(cog, _member(), channel, None)
        await asyncio.sleep(GRACE * 3)

        session.voice_client.disconnect.assert_not_awaited()

    async def test_dropping_below_min_users_stops_after_grace(
        self, cog: VoiceSummary
    ) -> None:
        channel = _channel(4)
        session = _recording(cog, channel)

        await _update(cog, _member(), channel, None)
        await asyncio.sleep(GRACE * 3)

        session.voice_client.disconnect.assert_awaited_once()

    async def test_rejoin_within_grace_keeps_recording(self, cog: VoiceSummary) -> None:
        channel = _channel(4)
        session = _recording(cog, channel)

        await _update(cog, _member(), channel, None)
        channel.members.append(MagicMock(bot=False))
        await _update(cog, _member(), None, channel)
        await asyncio.sleep(GRACE * 3)

        session.voice_client.disconnect.assert_not_awaited()

    async def test_mute_toggle_is_not_a_departure(self, cog: VoiceSummary) -> None:
        channel = _channel(3)
        session = _recording(cog, channel)

        await _update(cog, _member(), channel, channel)
        await asyncio.sleep(GRACE * 3)

        session.voice_client.disconnect.assert_not_awaited()

    async def test_last_member_leaving_stops_immediately(
        self, cog: VoiceSummary
    ) -> None:
        channel = _channel(0)
        session = _recording(cog, channel)

        await _update(cog, _member(), channel, None)

        session.voice_client.disconnect.assert_awaited_once()


class TestConnectionLoss:
    async def _lose_connection(self, cog: VoiceSummary, channel: MagicMock) -> None:
        assert cog.bot.user is not None
        bot_member = _member()
        bot_member.id = cog.bot.user.id
        await _update(cog, bot_member, channel, None)
        assert cog._resume_task is not None
        await cog._resume_task

    async def test_reuses_client_that_discord_py_is_reconnecting(
        self, cog: VoiceSummary
    ) -> None:
        channel = _channel(6)
        session = _recording(cog, channel, connected=False)
        channel.guild.voice_client = session.voice_client
        channel.connect = AsyncMock()

        await self._lose_connection(cog, channel)

        channel.connect.assert_not_awaited()
        session.resume.assert_awaited_once_with(session.voice_client)
        session.finalize.assert_not_awaited()

    async def test_connects_again_when_client_is_gone(self, cog: VoiceSummary) -> None:
        channel = _channel(6)
        session = _recording(cog, channel, connected=False)
        channel.guild.voice_client = None
        new_vc = _voice_client(channel)
        channel.connect = AsyncMock(return_value=new_vc)

        await self._lose_connection(cog, channel)

        session.resume.assert_awaited_once_with(new_vc)

    async def test_stop_while_reconnecting_finalizes(self, cog: VoiceSummary) -> None:
        session = _recording(cog, _channel(6), connected=False)

        await cog._disconnect_voice()

        session.finalize.assert_awaited_once()
        assert cog._session is None


class TestSessionVoiceClient:
    async def test_resume_keeps_existing_listener(self) -> None:
        session = CallSession(MagicMock(), make_bot())
        session.recorder = MagicMock()
        vc = _voice_client(_channel(1))
        vc.is_listening.return_value = True

        await session.resume(vc)

        vc.listen.assert_not_called()

    async def test_resume_attaches_listener_when_missing(self) -> None:
        session = CallSession(MagicMock(), make_bot())
        session.recorder = MagicMock()
        vc = _voice_client(_channel(1))
        vc.is_listening.return_value = False

        await session.resume(vc)

        vc.listen.assert_called_once()

    async def test_stop_mid_reconnect_forces_disconnect(self) -> None:
        # otherwise discord.py rejoins the call after the session is finalized
        session = CallSession(MagicMock(), make_bot())
        vc = _voice_client(_channel(1), connected=False)
        vc.guild.voice_client = vc
        session.voice_client = vc

        await session.stop()

        vc.disconnect.assert_awaited_once_with(force=True)
