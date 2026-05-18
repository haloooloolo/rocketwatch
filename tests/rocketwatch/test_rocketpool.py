from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_abi import abi

from rocketwatch.utils.rocketpool import RocketPool


class TestAbiTypeStr:
    def test_simple_type_returns_unchanged(self):
        assert RocketPool._abi_type_str({"type": "uint256"}) == "uint256"
        assert RocketPool._abi_type_str({"type": "bool"}) == "bool"
        assert RocketPool._abi_type_str({"type": "address"}) == "address"

    def test_flat_tuple_renders_paren_form(self):
        # Two-field tuple of (uint256, address).
        out = RocketPool._abi_type_str(
            {
                "type": "tuple",
                "components": [
                    {"type": "uint256"},
                    {"type": "address"},
                ],
            }
        )
        assert out == "(uint256,address)"

    def test_tuple_array_preserves_suffix(self):
        # `tuple[]` should produce `(...)[]`.
        out = RocketPool._abi_type_str(
            {
                "type": "tuple[]",
                "components": [{"type": "uint256"}],
            }
        )
        assert out == "(uint256)[]"

    def test_tuple_fixed_array_preserves_suffix(self):
        # `tuple[3]` should produce `(...)[3]`.
        out = RocketPool._abi_type_str(
            {
                "type": "tuple[3]",
                "components": [{"type": "uint256"}, {"type": "bool"}],
            }
        )
        assert out == "(uint256,bool)[3]"

    def test_nested_tuples_recurse(self):
        # tuple(uint256, tuple(address, bool)) → (uint256,(address,bool))
        out = RocketPool._abi_type_str(
            {
                "type": "tuple",
                "components": [
                    {"type": "uint256"},
                    {
                        "type": "tuple",
                        "components": [
                            {"type": "address"},
                            {"type": "bool"},
                        ],
                    },
                ],
            }
        )
        assert out == "(uint256,(address,bool))"


class TestDecodeFnOutput:
    def test_no_outputs_returns_none(self):
        fn = MagicMock()
        fn.abi = {"outputs": []}
        assert RocketPool._decode_fn_output(fn, b"") is None

    def test_single_output_returns_unwrapped_value(self):
        fn = MagicMock()
        fn.abi = {"outputs": [{"type": "uint256"}]}
        data = abi.encode(["uint256"], [42])
        assert RocketPool._decode_fn_output(fn, data) == 42

    def test_multiple_outputs_returns_tuple(self):
        fn = MagicMock()
        fn.abi = {"outputs": [{"type": "uint256"}, {"type": "bool"}]}
        data = abi.encode(["uint256", "bool"], [42, True])
        assert RocketPool._decode_fn_output(fn, data) == (42, True)

    def test_tuple_output_decoded_via_paren_form(self):
        # The function flips `tuple` ABI specs into eth_abi's paren syntax,
        # then decodes via that. End-to-end round-trip with a flat tuple.
        fn = MagicMock()
        fn.abi = {
            "outputs": [
                {
                    "type": "tuple",
                    "components": [{"type": "uint256"}, {"type": "address"}],
                }
            ]
        }
        address = "0x" + "11" * 20
        data = abi.encode(["(uint256,address)"], [(42, address)])
        result = RocketPool._decode_fn_output(fn, data)
        # Single-output unwrap → the inner tuple.
        assert result == (42, address)


class TestNormalizeCalls:
    def _make_fn(self) -> MagicMock:
        return MagicMock(name="fn")

    def test_plain_function_uses_default_require_success(self):
        fn = self._make_fn()
        fns, flags = RocketPool._normalize_calls([fn], default_require_success=True)
        assert fns == [fn]
        # `flags` records *allow_failure* (the inverse of require_success).
        assert flags == [False]

    def test_default_require_success_false_yields_allow_failure_true(self):
        fn = self._make_fn()
        _, flags = RocketPool._normalize_calls([fn], default_require_success=False)
        assert flags == [True]

    def test_per_call_override_wins_over_default(self):
        fn1 = self._make_fn()
        fn2 = self._make_fn()
        # Default is require_success=True, but the second call overrides to False.
        fns, flags = RocketPool._normalize_calls(
            [fn1, (fn2, False)], default_require_success=True
        )
        assert fns == [fn1, fn2]
        # fn1 → require=True → allow_failure=False
        # fn2 → require=False → allow_failure=True
        assert flags == [False, True]

    def test_empty_input_returns_empty_pair(self):
        fns, flags = RocketPool._normalize_calls([], default_require_success=True)
        assert fns == []
        assert flags == []


class TestMulticallShortCircuits:
    async def test_empty_calls_returns_empty_list(self):
        # `multicall` is called as a method on an instance; we just need the
        # short-circuit to not touch `_multicall`.
        rp_instance = RocketPool()
        assert await rp_instance.multicall([]) == []


class TestGetRevertReason:
    async def test_joins_contract_logic_error_args(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # web3 raises ContractLogicError(message, data). The cog joins both
        # args with ", " for the reported reason.
        from web3.exceptions import ContractLogicError

        from rocketwatch.utils import rocketpool as rp_module

        err = ContractLogicError("execution reverted: bad", "0xdeadbeef")
        rp_module.w3.eth.call = AsyncMock(side_effect=err)

        txn = {
            "from": "0xa",
            "to": "0xb",
            "input": "0x",
            "gas": 1,
            "gasPrice": 1,
            "value": 0,
            "blockNumber": 1,
            "hash": "0xdead",
        }
        reason = await RocketPool.get_revert_reason(txn)  # type: ignore[arg-type]
        assert "execution reverted: bad" in reason
        assert "0xdeadbeef" in reason

    async def test_out_of_gas_value_error_code(self, monkeypatch: pytest.MonkeyPatch):
        from rocketwatch.utils import rocketpool as rp_module

        rp_module.w3.eth.call = AsyncMock(side_effect=ValueError({"code": -32000}))

        txn = {
            "from": "0xa",
            "to": "0xb",
            "input": "0x",
            "gas": 1,
            "gasPrice": 1,
            "value": 0,
            "blockNumber": 1,
            "hash": "0xdead",
        }
        assert await RocketPool.get_revert_reason(txn) == "Out of gas"  # type: ignore[arg-type]

    async def test_unknown_value_error_code_returns_hidden_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from rocketwatch.utils import rocketpool as rp_module

        rp_module.w3.eth.call = AsyncMock(side_effect=ValueError({"code": -99999}))

        txn = {
            "from": "0xa",
            "to": "0xb",
            "input": "0x",
            "gas": 1,
            "gasPrice": 1,
            "value": 0,
            "blockNumber": 1,
            "hash": "0xdead",
        }
        assert await RocketPool.get_revert_reason(txn) == "Hidden Error"  # type: ignore[arg-type]

    async def test_no_revert_returns_unknown(self, monkeypatch: pytest.MonkeyPatch):
        from rocketwatch.utils import rocketpool as rp_module

        rp_module.w3.eth.call = AsyncMock(return_value=b"")

        txn = {
            "from": "0xa",
            "to": "0xb",
            "input": "0x",
            "gas": 1,
            "gasPrice": 1,
            "value": 0,
            "blockNumber": 1,
            "hash": "0xdead",
        }
        assert await RocketPool.get_revert_reason(txn) == "Unknown"  # type: ignore[arg-type]
