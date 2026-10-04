import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_abi import abi
from hexbytes import HexBytes
from web3 import AsyncWeb3

from rocketwatch.utils import rocketpool as rp_module
from rocketwatch.utils import shared_w3
from rocketwatch.utils.rocketpool import RocketPool, _calldata, at_address

_INFO_ABI = [
    {
        "type": "function",
        "name": "getValidatorInfo",
        "stateMutability": "view",
        "inputs": [{"name": "id", "type": "uint32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]
_ADDR_A = AsyncWeb3.to_checksum_address("0x" + "aa" * 20)
_ADDR_B = AsyncWeb3.to_checksum_address("0x" + "bb" * 20)
_MULTICALL = AsyncWeb3.to_checksum_address("0x" + "cc" * 20)


class TestAtAddress:
    def test_targets_new_address_with_same_calldata(self) -> None:
        template = AsyncWeb3().eth.contract(abi=_INFO_ABI)
        fn = template.functions.getValidatorInfo(7)
        bound = at_address(fn, _ADDR_A)
        assert bound.address == _ADDR_A
        assert bound._encode_transaction_data() == fn._encode_transaction_data()
        assert bound.abi == fn.abi

    def test_leaves_template_untouched(self) -> None:
        template = AsyncWeb3().eth.contract(address=_ADDR_A, abi=_INFO_ABI)
        fn = template.functions.getValidatorInfo(7)
        at_address(fn, _ADDR_B)
        assert fn.address == _ADDR_A


_ENCODE_ABI = [
    {
        "type": "function",
        "name": n,
        "stateMutability": "view",
        "inputs": i,
        "outputs": [],
    }
    for n, i in [
        ("none", []),
        (
            "scalars",
            [
                {"name": "a", "type": "address"},
                {"name": "b", "type": "uint256"},
                {"name": "c", "type": "bytes32"},
                {"name": "d", "type": "string"},
                {"name": "e", "type": "bool"},
            ],
        ),
        (
            "nested",
            [
                {
                    "name": "t",
                    "type": "tuple",
                    "components": [
                        {"name": "x", "type": "address"},
                        {"name": "y", "type": "uint64[]"},
                    ],
                },
                {"name": "z", "type": "bytes"},
            ],
        ),
        ("hash", [{"name": "h", "type": "bytes32"}]),
    ]
]


class TestCalldata:
    @pytest.mark.parametrize(
        ("method", "args"),
        [
            ("none", ()),
            ("scalars", (_ADDR_A, 5, b"\x01" * 32, "hi", True)),
            ("nested", ((_ADDR_A, (1, 2)), b"xyz")),
            # a list arg is unhashable, so it bypasses the cache
            ("nested", ((_ADDR_A, [1, 2]), b"xyz")),
            # hex string for bytes32 is only accepted by web3's normalizers
            ("hash", ("0x" + "11" * 32,)),
        ],
    )
    def test_matches_web3_encoding(self, method: str, args: tuple[Any, ...]) -> None:
        contract = AsyncWeb3().eth.contract(abi=_ENCODE_ABI)
        fn = contract.functions[method](*args)
        assert _calldata(fn) == HexBytes(fn._encode_transaction_data())
        # second lookup may come from the cache and must be identical
        assert _calldata(fn) == HexBytes(fn._encode_transaction_data())


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


class TestMulticall:
    @pytest.fixture
    def rp_instance(self, monkeypatch: pytest.MonkeyPatch) -> RocketPool:
        # Simulated Multicall3: getValidatorInfo(id) returns id * 10 at _ADDR_A
        # and reverts at _ADDR_B.
        async def eth_call(tx: dict[str, Any], block_identifier: Any) -> bytes:
            assert tx["to"] == _MULTICALL
            (calls,) = abi.decode(["(address,bool,bytes)[]"], tx["data"][4:])
            results = []
            for target, allow_failure, calldata in calls:
                if target.lower() != _ADDR_A.lower():
                    assert allow_failure
                    results.append((False, b""))
                    continue
                (validator_id,) = abi.decode(["uint32"], calldata[4:])
                results.append((True, abi.encode(["uint256"], [validator_id * 10])))
            return abi.encode(["(bool,bytes)[]"], [results])

        monkeypatch.setattr(shared_w3.w3.eth, "call", eth_call)
        instance = RocketPool()
        instance._multicall = MagicMock(address=_MULTICALL)
        return instance

    async def test_decodes_each_result_and_nones_allowed_failures(
        self, rp_instance: RocketPool
    ) -> None:
        a = AsyncWeb3().eth.contract(address=_ADDR_A, abi=_INFO_ABI)
        b = AsyncWeb3().eth.contract(address=_ADDR_B, abi=_INFO_ABI)
        results = await rp_instance.multicall(
            [
                a.functions.getValidatorInfo(1),
                (b.functions.getValidatorInfo(2), False),
                a.functions.getValidatorInfo(3),
            ]
        )
        assert results == [10, None, 30]


class TestAssembleContractCache:
    _ABI = json.dumps(
        [
            {
                "type": "function",
                "name": "getNodeCount",
                "stateMutability": "view",
                "inputs": [],
                "outputs": [{"name": "", "type": "uint256"}],
            }
        ]
    )
    _A = AsyncWeb3.to_checksum_address("0x" + "aa" * 20)
    _B = AsyncWeb3.to_checksum_address("0x" + "bb" * 20)

    @pytest.fixture
    def rp_instance(self, monkeypatch: pytest.MonkeyPatch) -> RocketPool:
        monkeypatch.setattr(rp_module, "w3", AsyncWeb3())
        instance = RocketPool()
        monkeypatch.setattr(
            instance, "get_abi_by_name", AsyncMock(return_value=self._ABI)
        )
        monkeypatch.setattr(instance, "_init_contract_addresses", AsyncMock())
        return instance

    async def test_reuses_contract_for_same_name_and_address(
        self, rp_instance: RocketPool
    ) -> None:
        first = await rp_instance.assemble_contract("rocketNodeManager", self._A)
        second = await rp_instance.assemble_contract("rocketNodeManager", self._A)
        assert first is second
        assert first.address == self._A

    async def test_distinct_addresses_get_distinct_contracts(
        self, rp_instance: RocketPool
    ) -> None:
        a = await rp_instance.assemble_contract("rocketMinipool", self._A)
        b = await rp_instance.assemble_contract("rocketMinipool", self._B)
        assert a is not b
        assert (a.address, b.address) == (self._A, self._B)

    async def test_flush_rebuilds_contracts(self, rp_instance: RocketPool) -> None:
        before = await rp_instance.assemble_contract("rocketNodeManager", self._A)
        await rp_instance.flush()
        after = await rp_instance.assemble_contract("rocketNodeManager", self._A)
        assert before is not after


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
