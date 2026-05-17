from typing import cast

import pytest
from eth_typing import ChecksumAddress

from rocketwatch.utils.rocketpool import rp
from tests.support.scripted_rocketpool import ScriptedRocketPool, addr


class TestScriptedResponses:
    async def test_call_returns_scripted_constant(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        scripted_rp.set_call("rocketTokenRPL.getInflationIntervalRate", 1_000_000)
        assert await rp.call("rocketTokenRPL.getInflationIntervalRate") == 1_000_000

    async def test_call_supports_arg_dependent_response(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        scripted_rp.set_call(
            "rocketNodeStaking.getNodeStakedRPL",
            lambda address: 10 if address == "0xAAA" else 0,
        )
        assert await rp.call("rocketNodeStaking.getNodeStakedRPL", "0xAAA") == 10
        assert await rp.call("rocketNodeStaking.getNodeStakedRPL", "0xBBB") == 0

    async def test_unscripted_call_raises_descriptive_error(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        # Surface a clear error rather than silently returning None — tests
        # should explicitly opt into every call they trigger.
        with pytest.raises(KeyError, match=r"rocketTokenRPL\.getInflationIntervalRate"):
            await rp.call("rocketTokenRPL.getInflationIntervalRate")

    async def test_is_node_reflects_marked_set(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        scripted_rp.mark_node(addr("0xNODE"))
        assert await rp.is_node(cast(ChecksumAddress, "0xNODE")) is True
        assert await rp.is_node(cast(ChecksumAddress, "0xOTHER")) is False

    async def test_address_round_trip(self, scripted_rp: ScriptedRocketPool) -> None:
        scripted_rp.set_address("rocketDepositPool", addr("0xDEPOSIT"))
        assert (await rp.get_address_by_name("rocketDepositPool")) == "0xDEPOSIT"
        assert rp.get_name_by_address(cast(ChecksumAddress, "0xDEPOSIT")) == (
            "rocketDepositPool"
        )


class TestProxyVisibility:
    """The proxy refactor's load-bearing claim is that `rp._instance = X`
    propagates through every `from rocketwatch.utils.rocketpool import rp`
    call site, even ones that imported `rp` before the test ran."""

    async def test_consumer_module_sees_swapped_instance(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        # `embeds` imported `rp` at module load via `from ... import rp`.
        # If the proxy were doing instance shadowing wrong, embeds would still
        # see the original RocketPool here.
        from rocketwatch.utils import embeds

        embeds_rp = embeds.rp  # type: ignore[attr-defined]
        scripted_rp.set_call("rocketTokenRPL.getInflationIntervalRate", 42)
        assert await embeds_rp.call("rocketTokenRPL.getInflationIntervalRate") == 42
