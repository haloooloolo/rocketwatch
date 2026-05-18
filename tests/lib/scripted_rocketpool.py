from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from eth_typing import ChecksumAddress
from web3.constants import ADDRESS_ZERO

# A scripted response can be a constant, or a callable receiving the args
# passed to `rp.call(...)` so a test can return different values per-arg.
ScriptedResponse = Any | Callable[..., Any]


class _ScriptedCall:
    """A `contract.functions.foo(...)` stand-in. Resolves via the parent
    ScriptedRocketPool's `_calls` map on `.call(...)`."""

    def __init__(
        self,
        rp: ScriptedRocketPool,
        path: str,
        args: tuple[Any, ...],
    ) -> None:
        self._rp = rp
        self._path = path
        self._args = args

    async def call(self, block_identifier: Any = "latest") -> Any:
        return await self._rp.call(self._path, *self._args)


class _ScriptedFunctions:
    """The `contract.functions` namespace; `__getattr__` makes any method
    name yield a callable that builds a `_ScriptedCall`."""

    def __init__(self, rp: ScriptedRocketPool, contract_name: str) -> None:
        self._rp = rp
        self._contract = contract_name

    def __getattr__(self, method: str) -> Callable[..., _ScriptedCall]:
        def factory(*args: Any) -> _ScriptedCall:
            return _ScriptedCall(self._rp, f"{self._contract}.{method}", args)

        return factory


class _ScriptedContract:
    """A `contract` stand-in. `functions.<method>(...)` returns a `_ScriptedCall`."""

    def __init__(self, rp: ScriptedRocketPool, contract_name: str) -> None:
        self.functions = _ScriptedFunctions(rp, contract_name)


class ScriptedRocketPool:
    """Drop-in for `rp` that returns scripted values. Substitute via
    `rp._instance = ScriptedRocketPool()` (or the `scripted_rp` fixture)."""

    def __init__(self) -> None:
        self._calls: dict[str, ScriptedResponse] = {}
        self._addresses: dict[str, ChecksumAddress] = {}
        self._names: dict[ChecksumAddress, str] = {}
        self._nodes: set[ChecksumAddress] = set()
        self._megapools: set[ChecksumAddress] = set()
        self._minipools: set[ChecksumAddress] = set()
        self._strings: dict[str, str] = {}
        self._uints: dict[str, int] = {}

    def set_call(self, method: str, value: ScriptedResponse) -> None:
        self._calls[method] = value

    def set_address(self, name: str, address: ChecksumAddress) -> None:
        self._addresses[name] = address
        self._names[address] = name

    def set_string(self, key: str, value: str) -> None:
        self._strings[key] = value

    def set_uint(self, key: str, value: int) -> None:
        self._uints[key] = value

    def mark_node(self, address: ChecksumAddress) -> None:
        self._nodes.add(address)

    def mark_minipool(self, address: ChecksumAddress) -> None:
        self._minipools.add(address)

    def mark_megapool(self, address: ChecksumAddress) -> None:
        self._megapools.add(address)

    async def call(
        self,
        path: str,
        *args: Any,
        block: Any = "latest",
        address: ChecksumAddress | None = None,
        mainnet: bool = False,
    ) -> Any:
        if path not in self._calls:
            raise KeyError(
                f"ScriptedRocketPool.call: no response scripted for {path!r}. "
                f"Use rp.set_call({path!r}, <value>) in the test setup."
            )
        value = self._calls[path]
        return value(*args) if callable(value) else value

    async def multicall(
        self,
        calls: list[Any],
        require_success: bool = True,
        block: Any = "latest",
    ) -> list[Any]:
        # Each call may be a plain ContractFunction (real or scripted) or an
        # (fn, require_success) tuple; both shapes have `.call()`.
        results: list[Any] = []
        for entry in calls:
            fn = entry[0] if isinstance(entry, tuple) else entry
            results.append(await fn.call(block_identifier=block))
        return results

    async def get_address_by_name(self, name: str) -> ChecksumAddress:
        if name not in self._addresses:
            raise KeyError(f"No scripted address for contract {name!r}")
        return self._addresses[name]

    async def uncached_get_address_by_name(
        self, name: str, block: Any = "latest"
    ) -> ChecksumAddress:
        # Cache distinction doesn't matter for scripted tests.
        return await self.get_address_by_name(name)

    def get_name_by_address(self, address: ChecksumAddress) -> str | None:
        return self._names.get(address)

    async def get_contract_by_name(
        self, name: str, mainnet: bool = False
    ) -> _ScriptedContract:
        return _ScriptedContract(self, name)

    async def assemble_contract(
        self,
        name: str,
        address: ChecksumAddress | None = None,
        mainnet: bool = False,
    ) -> _ScriptedContract:
        return _ScriptedContract(self, name)

    async def is_node(self, address: ChecksumAddress) -> bool:
        return address in self._nodes

    async def is_minipool(self, address: ChecksumAddress) -> bool:
        return address in self._minipools

    async def is_megapool(self, address: ChecksumAddress) -> bool:
        return address in self._megapools

    async def get_string(self, key: str) -> str:
        return self._strings.get(key, "")

    async def get_uint(self, key: str) -> int:
        return self._uints.get(key, 0)

    async def async_init(self) -> None:
        # Real RocketPool reads contract addresses from chain here; scripted
        # version is already fully populated by the test.
        return None

    async def flush(self) -> None:
        self._calls.clear()
        self._addresses.clear()
        self._names.clear()
        self._nodes.clear()
        self._megapools.clear()
        self._minipools.clear()
        self._strings.clear()
        self._uints.clear()


def addr(value: str) -> ChecksumAddress:
    """Cast a 0x-string literal to a ChecksumAddress (no checksum validation)."""
    return cast(ChecksumAddress, value)


ADDRESS_ZERO_CS: ChecksumAddress = cast(ChecksumAddress, ADDRESS_ZERO)
