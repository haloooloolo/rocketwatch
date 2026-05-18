from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from rocketwatch.plugins.call.call import Call, CallModal
from tests.lib.discord_harness import make_bot, make_interaction
from tests.lib.scripted_rocketpool import ScriptedRocketPool


@pytest.fixture
def _stub_w3_is_address(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The baseline w3 is a MagicMock; calling `is_address(x)` on it returns
    # a MagicMock (truthy), so any input passes validation. Give it a real
    # check so the invalid-address branch can actually fire.
    def is_address(value: Any) -> bool:
        return isinstance(value, str) and value.startswith("0x") and len(value) == 42

    monkeypatch.setattr("rocketwatch.plugins.call.call.w3.is_address", is_address)
    monkeypatch.setattr(
        "rocketwatch.plugins.call.call.w3.to_checksum_address", lambda v: v
    )
    yield


class TestCallModalValidate:
    @pytest.mark.parametrize(
        ("value", "abi_type", "expect_error"),
        [
            (True, "bool", False),
            (False, "bool", False),
            ("not-a-bool", "bool", True),
            ("0x" + "ab" * 20, "address", False),
            ("not-an-address", "address", True),
            ("hello", "string", False),
            (123, "string", True),
            (42, "uint256", False),
            (-1, "int128", False),
            ("42", "uint256", True),
            (True, "uint256", True),  # bools must not pass as ints
            (b"\x01\x02", "bytes32", False),
            ([1, 2, 3], "bytes", False),
            ("0xabcd", "bytes", False),
            ("no_prefix", "bytes32", True),
            (42, "bytes", True),
        ],
    )
    def test_validation_outcomes(
        self,
        value: Any,
        abi_type: str,
        expect_error: bool,
        _stub_w3_is_address: None,
    ) -> None:
        result = CallModal._validate(value, abi_type)
        if expect_error:
            assert result is not None
            assert "expected" in result
        else:
            assert result is None


class TestCallCommandBlockParsing:
    async def test_numeric_block_passes_to_executor(
        self,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `_execute_call` invokes rp.call with the full "contract.func()" path.
        scripted_rp.set_call("rocketDAONodeTrusted.getMemberCount()", 7)
        # Stub the gas estimator (not part of ScriptedRocketPool's surface).
        monkeypatch.setattr(
            scripted_rp,
            "estimate_gas_for_call",
            AsyncMock(return_value=10_000),
            raising=False,
        )

        cog = Call(make_bot())
        interaction = make_interaction()
        # No ABI inputs for this contract in the scripted setup → bypasses
        # modal and runs _execute_call directly.
        await cog.call.callback(
            cog,
            interaction,
            function="rocketDAONodeTrusted.getMemberCount()",
            block="12345",
        )

        interaction.followup.send.assert_awaited_once()
        sent = interaction.followup.send.call_args
        # The block identifier shows up in the rendered response text.
        content = sent.kwargs.get("content") or (sent.args[0] if sent.args else "")
        assert "block: 12345" in content
        assert "7" in content

    async def test_named_block_keyword_accepted(
        self,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scripted_rp.set_call("rocketDAONodeTrusted.getMemberCount()", 5)
        monkeypatch.setattr(
            scripted_rp,
            "estimate_gas_for_call",
            AsyncMock(return_value=1_000),
            raising=False,
        )

        cog = Call(make_bot())
        interaction = make_interaction()
        await cog.call.callback(
            cog,
            interaction,
            function="rocketDAONodeTrusted.getMemberCount()",
            block="finalized",
        )
        # Just verify the call landed — block parsing didn't reject.
        interaction.followup.send.assert_awaited_once()

    async def test_invalid_block_rejected_inline(self) -> None:
        cog = Call(make_bot())
        interaction = make_interaction()
        await cog.call.callback(
            cog,
            interaction,
            function="x.y()",
            block="nonsense",
        )
        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.call_args.args[0]
        assert "Invalid block" in msg

    async def test_invalid_address_rejected_inline(
        self, _stub_w3_is_address: None
    ) -> None:
        cog = Call(make_bot())
        interaction = make_interaction()
        await cog.call.callback(
            cog,
            interaction,
            function="x.y()",
            block="latest",
            address="not-an-address",
        )
        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.call_args.args[0]
        assert "Invalid contract address" in msg


class TestExecuteCall:
    async def test_exception_path_reports_repr(
        self,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Unscripted function → ScriptedRocketPool.call raises KeyError.
        monkeypatch.setattr(
            scripted_rp,
            "estimate_gas_for_call",
            AsyncMock(return_value=0),
            raising=False,
        )
        cog = Call(make_bot())
        interaction = make_interaction()
        await cog._execute_call(
            interaction,
            function="unscripted.fn()",
            args=[],
            block="latest",
            address=None,
            raw_output=False,
        )
        sent = interaction.followup.send.call_args
        content = sent.kwargs.get("content") or sent.args[0]
        assert "Exception:" in content

    async def test_large_int_result_scaled_to_float(
        self,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 10**18 (1 ETH) → solidity.to_float → 1.0; the rendered text
        # depends on the float representation, so verify the float branch
        # is taken (no full 18-digit number in the output).
        scripted_rp.set_call("token.balanceOf", 10**18)
        monkeypatch.setattr(
            scripted_rp,
            "estimate_gas_for_call",
            AsyncMock(return_value=21_000),
            raising=False,
        )
        cog = Call(make_bot())
        interaction = make_interaction()
        await cog._execute_call(
            interaction,
            function="token.balanceOf",
            args=[],
            block="latest",
            address=None,
            raw_output=False,
        )
        sent = interaction.followup.send.call_args
        content = sent.kwargs.get("content") or sent.args[0]
        assert "1.0" in content

    async def test_raw_output_keeps_int_as_is(
        self,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scripted_rp.set_call("token.balanceOf", 10**18)
        monkeypatch.setattr(
            scripted_rp,
            "estimate_gas_for_call",
            AsyncMock(return_value=21_000),
            raising=False,
        )
        cog = Call(make_bot())
        interaction = make_interaction()
        await cog._execute_call(
            interaction,
            function="token.balanceOf",
            args=[],
            block="latest",
            address=None,
            raw_output=True,
        )
        content = (
            interaction.followup.send.call_args.kwargs.get("content")
            or interaction.followup.send.call_args.args[0]
        )
        # raw_output: int is rendered verbatim, no float scaling.
        assert "1000000000000000000" in content

    async def test_gas_estimate_failure_falls_back_to_na(
        self,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        scripted_rp.set_call("token.balanceOf", 42)

        async def boom(*_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError("rpc down")

        monkeypatch.setattr(scripted_rp, "estimate_gas_for_call", boom, raising=False)
        cog = Call(make_bot())
        interaction = make_interaction()
        await cog._execute_call(
            interaction,
            function="token.balanceOf",
            args=[],
            block="latest",
            address=None,
            raw_output=False,
        )
        content = (
            interaction.followup.send.call_args.kwargs.get("content")
            or interaction.followup.send.call_args.args[0]
        )
        assert "gas estimate: N/A" in content

    async def test_gas_estimate_revert_falls_back_to_na(
        self,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The cog has a special branch intended to surface the revert message
        # from eth_estimateGas, but as written the condition `"code" in err.args`
        # checks the wrong container (the tuple, not the dict inside it), so
        # the message is never attached. Pin the actual behaviour: gas estimate
        # falls back to plain "N/A" without the revert reason.
        scripted_rp.set_call("token.balanceOf", 1)
        revert = ValueError({"code": -32000, "message": "execution reverted"})

        async def reverting(*_args: Any, **_kwargs: Any) -> int:
            raise revert

        monkeypatch.setattr(
            scripted_rp, "estimate_gas_for_call", reverting, raising=False
        )
        cog = Call(make_bot())
        interaction = make_interaction()
        await cog._execute_call(
            interaction,
            function="token.balanceOf",
            args=[],
            block="latest",
            address=None,
            raw_output=False,
        )
        content = (
            interaction.followup.send.call_args.kwargs.get("content")
            or interaction.followup.send.call_args.args[0]
        )
        assert "gas estimate: N/A" in content
        assert "execution reverted" not in content

    async def test_oversized_result_attached_as_file(
        self,
        scripted_rp: ScriptedRocketPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Result longer than the 2000-char threshold triggers the file path.
        scripted_rp.set_call("contract.bigBlob", "x" * 3000)
        monkeypatch.setattr(
            scripted_rp,
            "estimate_gas_for_call",
            AsyncMock(return_value=42),
            raising=False,
        )
        cog = Call(make_bot())
        interaction = make_interaction()
        await cog._execute_call(
            interaction,
            function="contract.bigBlob",
            args=[],
            block="latest",
            address=None,
            raw_output=True,
        )
        sent = interaction.followup.send.call_args
        # The cog sends both a content blurb AND a `file=` attachment.
        assert "too long" in sent.args[0] or "too long" in sent.kwargs.get(
            "content", ""
        )
        assert "file" in sent.kwargs


class TestMatchFunctionName:
    async def test_filters_function_names_case_insensitive(self) -> None:
        cog = Call(make_bot())
        cog.function_names = [
            "rocketDAONodeTrusted.getMemberCount()",
            "rocketTokenRPL.balanceOf(address)",
            "rocketTokenRETH.balanceOf(address)",
        ]
        out = await cog.match_function_name(make_interaction(), "rpl")
        assert len(out) == 1
        assert "RPL" in out[0].name

    async def test_caps_results_at_25(self) -> None:
        cog = Call(make_bot())
        cog.function_names = [f"contract.fn{i}()" for i in range(40)]
        out = await cog.match_function_name(make_interaction(), "fn")
        assert len(out) == 25
