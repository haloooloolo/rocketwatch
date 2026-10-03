import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord import TextChannel, ui
from discord.ext.voice_recv import VoiceRecvClient

from rocketwatch.plugins.voice_summary import voice_summary as vs_mod
from rocketwatch.plugins.voice_summary.session import CallResult, CallSession
from rocketwatch.plugins.voice_summary.voice_summary import VoiceSummary
from rocketwatch.utils.config import LLMConfig, STTConfig, TranscriptionConfig, cfg
from tests.lib.discord_harness import make_bot, make_interaction

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


def _result(tmp_path: Path, *, audio_bytes: int = 100, summary: str = "") -> CallResult:
    audio = tmp_path / "recording.mp3"
    audio.write_bytes(bytes(audio_bytes))
    return CallResult(
        transcript="[0:00] Alice: gm",
        summary=summary or "**Topics Discussed**\n- <@111> proposed a fee change",
        audio_path=audio,
    )


def _bot(cog: VoiceSummary) -> MagicMock:
    return cast(MagicMock, cog.bot)


def _output_channel(cog: VoiceSummary, *, upload_limit: int = 10_000) -> MagicMock:
    channel = MagicMock(spec=TextChannel)
    channel.guild.filesize_limit = upload_limit
    channel.send = AsyncMock()
    _bot(cog).get_or_fetch_channel = AsyncMock(return_value=channel)
    cog._config.output_channel_id = 555
    return channel


def _texts(view: ui.LayoutView) -> list[str]:
    return [c.content for c in view.walk_children() if isinstance(c, ui.TextDisplay)]


class TestPostResults:
    async def test_posts_summary_with_recording_and_transcript(
        self, cog: VoiceSummary, tmp_path: Path
    ) -> None:
        channel = _output_channel(cog)

        await cog._post_results(_result(tmp_path))

        kwargs = channel.send.call_args.kwargs
        assert [f.filename for f in kwargs["files"]] == [
            "recording.mp3",
            "transcript.txt",
        ]
        assert any("proposed a fee change" in t for t in _texts(kwargs["view"]))
        # the summary mentions participants; posting it must not ping them
        assert kwargs["allowed_mentions"].users is False

    async def test_recording_over_upload_limit_is_left_out(
        self, cog: VoiceSummary, tmp_path: Path
    ) -> None:
        channel = _output_channel(cog, upload_limit=1_000)

        await cog._post_results(_result(tmp_path, audio_bytes=2_000))

        files = channel.send.call_args.kwargs["files"]
        assert [f.filename for f in files] == ["transcript.txt"]

    async def test_long_summary_fits_discord_text_limit(
        self, cog: VoiceSummary, tmp_path: Path
    ) -> None:
        channel = _output_channel(cog)

        await cog._post_results(_result(tmp_path, summary="x" * 5_000))

        view = channel.send.call_args.kwargs["view"]
        assert sum(len(t) for t in _texts(view)) <= 4000

    async def test_without_output_channel_nothing_is_posted(
        self, cog: VoiceSummary, tmp_path: Path
    ) -> None:
        _bot(cog).get_or_fetch_channel = AsyncMock()

        await cog._post_results(_result(tmp_path))

        _bot(cog).get_or_fetch_channel.assert_not_awaited()


class TestFinishCall:
    async def test_finished_call_is_posted(
        self, cog: VoiceSummary, tmp_path: Path
    ) -> None:
        channel = _output_channel(cog)
        session = _recording(cog, _channel(0))
        session.finalize.return_value = _result(tmp_path)

        await cog._stop_recording()

        channel.send.assert_awaited_once()
        assert cog._session is None

    async def test_call_without_summary_posts_nothing(
        self, cog: VoiceSummary, tmp_path: Path
    ) -> None:
        channel = _output_channel(cog)
        _recording(cog, _channel(0))

        await cog._stop_recording()

        channel.send.assert_not_awaited()

    async def test_finalize_failure_is_reported(self, cog: VoiceSummary) -> None:
        session = _recording(cog, _channel(0))
        error = RuntimeError("stt down")
        session.finalize.side_effect = error

        await cog._stop_recording()

        _bot(cog).report_error.assert_awaited_once_with(error)
        assert cog._session is None


class TestCommands:
    async def test_start_while_recording_is_refused(self, cog: VoiceSummary) -> None:
        _recording(cog, _channel(6))
        interaction = make_interaction()
        target = MagicMock()
        target.guild.voice_client = None
        target.connect = AsyncMock()

        await cog.start_recording.callback(cog, interaction, target)  # type: ignore[arg-type, call-arg]

        assert interaction.followup.send.call_args.args[0] == "Already recording."
        target.connect.assert_not_awaited()

    async def test_start_joins_the_channel(self, cog: VoiceSummary) -> None:
        interaction = make_interaction()
        target = MagicMock()
        target.guild.voice_client = None
        target.connect = AsyncMock()

        await cog.start_recording.callback(cog, interaction, target)  # type: ignore[arg-type, call-arg]

        target.connect.assert_awaited_once_with(cls=VoiceRecvClient)

    async def test_stop_when_idle_is_refused(self, cog: VoiceSummary) -> None:
        interaction = make_interaction()

        await cog.stop_recording.callback(cog, interaction)  # type: ignore[arg-type, call-arg]

        assert interaction.followup.send.call_args.args[0] == "Not currently recording."

    async def test_stop_leaves_the_call(self, cog: VoiceSummary) -> None:
        session = _recording(cog, _channel(6))
        interaction = make_interaction()

        await cog.stop_recording.callback(cog, interaction)  # type: ignore[arg-type, call-arg]

        session.voice_client.disconnect.assert_awaited_once()
        assert interaction.followup.send.call_args.args[0] == "Recording stopped."


class TestAutoStart:
    async def test_enough_members_joining_starts_recording(
        self, cog: VoiceSummary
    ) -> None:
        channel = _channel(5)
        channel.guild.voice_client = None
        channel.connect = AsyncMock()

        await _update(cog, _member(), None, channel)

        channel.connect.assert_awaited_once_with(cls=VoiceRecvClient)

    async def test_small_group_is_not_recorded(self, cog: VoiceSummary) -> None:
        channel = _channel(4)
        channel.guild.voice_client = None
        channel.connect = AsyncMock()

        await _update(cog, _member(), None, channel)

        channel.connect.assert_not_awaited()

    async def test_bot_joining_starts_a_session(
        self, cog: VoiceSummary, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = MagicMock(start=AsyncMock())
        monkeypatch.setattr(vs_mod, "CallSession", MagicMock(return_value=session))
        channel = _channel(5)
        channel.guild.voice_client = _voice_client(channel)
        assert cog.bot.user is not None
        bot_member = _member()
        bot_member.id = cog.bot.user.id

        await _update(cog, bot_member, None, channel)

        session.start.assert_awaited_once_with(channel.guild.voice_client)
        assert cog._session is session
        cog._cancel_scheduled_tasks()

    async def test_unload_stops_without_posting(self, cog: VoiceSummary) -> None:
        session = _recording(cog, _channel(6))
        session.stop = AsyncMock()

        await cog.cog_unload()

        session.stop.assert_awaited_once()
        session.finalize.assert_not_awaited()
        assert cog._session is None
