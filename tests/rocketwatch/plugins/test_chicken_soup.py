from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from rocketwatch.plugins.chicken_soup.chicken_soup import ChickenSoup
from tests.lib.discord_harness import make_bot, make_interaction


@pytest.fixture
def cog() -> ChickenSoup:
    return ChickenSoup(make_bot())


class TestChickenSoupCommand:
    async def test_arms_the_dispenser_for_the_channel(self, cog: ChickenSoup) -> None:
        interaction = make_interaction(channel_id=42)
        await cog.chicken_soup.callback(cog, interaction)

        assert 42 in cog.dispense_end
        # The dispense window should expire ~`duration` from now (allow slack
        # for the wall-clock advance between the `.now()` call and the assert).
        delta = cog.dispense_end[42] - datetime.now()
        assert timedelta(seconds=0) < delta <= cog.duration

    async def test_sends_the_gif_link(self, cog: ChickenSoup) -> None:
        interaction = make_interaction(channel_id=42)
        await cog.chicken_soup.callback(cog, interaction)

        # The cog's only outgoing payload is a tenor link; the test pins the
        # behaviour (a single send_message call with a URL string), not the URL.
        interaction.response.send_message.assert_called_once()
        args, _kwargs = interaction.response.send_message.call_args
        assert args[0].startswith("https://")

    async def test_no_channel_id_skips_arming_but_still_replies(
        self, cog: ChickenSoup
    ) -> None:
        # The cog must respond even when invoked from a context without a
        # channel id (e.g. an ephemeral interaction); it just can't *arm*.
        interaction = make_interaction(channel_id=1)
        interaction.channel_id = None
        await cog.chicken_soup.callback(cog, interaction)

        assert cog.dispense_end == {}
        interaction.response.send_message.assert_called_once()


class TestOnMessageReactions:
    def _make_message(
        self,
        *,
        channel_id: int,
        is_bot: bool = False,
    ) -> MagicMock:
        msg = MagicMock()
        msg.channel.id = channel_id
        msg.add_reaction = AsyncMock()
        msg.author = "bot-user" if is_bot else "human-user"
        return msg

    async def test_adds_reactions_during_active_window(self, cog: ChickenSoup) -> None:
        cog.bot.user = "bot-user"
        cog.dispense_end[7] = datetime.now() + timedelta(minutes=1)

        message = self._make_message(channel_id=7)
        await cog.on_message(message)

        # Both 🐔 and 🍲 should be applied — the order is part of the user-visible
        # behaviour, so pin it.
        assert message.add_reaction.await_args_list == [
            (("🐔",),),
            (("🍲",),),
        ]
        # And the timer should *not* be cleared while still active.
        assert 7 in cog.dispense_end

    async def test_ignores_bot_self_messages(self, cog: ChickenSoup) -> None:
        cog.bot.user = "bot-user"
        cog.dispense_end[7] = datetime.now() + timedelta(minutes=1)

        message = self._make_message(channel_id=7, is_bot=True)
        await cog.on_message(message)

        message.add_reaction.assert_not_awaited()

    async def test_skips_channels_not_armed(self, cog: ChickenSoup) -> None:
        cog.bot.user = "bot-user"
        # dispense_end is empty — channel 9 has never been armed.
        message = self._make_message(channel_id=9)
        await cog.on_message(message)

        message.add_reaction.assert_not_awaited()

    async def test_clears_expired_window_without_reacting(
        self, cog: ChickenSoup
    ) -> None:
        cog.bot.user = "bot-user"
        cog.dispense_end[7] = datetime.now() - timedelta(seconds=1)

        message = self._make_message(channel_id=7)
        await cog.on_message(message)

        message.add_reaction.assert_not_awaited()
        # The expired entry should be purged so we don't accumulate dead
        # channel ids forever.
        assert 7 not in cog.dispense_end
