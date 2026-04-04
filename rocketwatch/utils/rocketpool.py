import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple, cast

from bidict import bidict
from cachetools import FIFOCache
from eth_abi import abi
from eth_typing import BlockIdentifier, ChecksumAddress
from web3.constants import ADDRESS_ZERO
from web3.contract import AsyncContract
from web3.contract.async_contract import AsyncContractFunction
from web3.exceptions import ContractLogicError
from web3.types import TxData

from rocketwatch.utils import solidity
from rocketwatch.utils.config import cfg
from rocketwatch.utils.readable import decode_abi
from rocketwatch.utils.shared_w3 import w3, w3_archive, w3_mainnet

log = logging.getLogger("rocketwatch.rocketpool")


class ValidatorInfo(NamedTuple):
    last_assignment_time: int
    last_requested_value: int
    last_requested_bond: int
    deposit_value: int
    staked: bool
    exited: bool
    in_queue: bool
    in_prestake: bool
    express_used: bool
    dissolved: bool
    exiting: bool
    locked: bool
    exit_balance: int
    locked_time: int


class NoAddressFound(Exception):
    pass


class RocketPool:
    ADDRESS_CACHE: FIFOCache[str, ChecksumAddress] = FIFOCache(maxsize=2048)
    ABI_CACHE: FIFOCache[str, str] = FIFOCache(maxsize=2048)
    CONTRACT_CACHE: FIFOCache[tuple, AsyncContract] = FIFOCache(maxsize=2048)

    def __init__(self) -> None:
        self.addresses: bidict[str, ChecksumAddress] = bidict()
        self._multicall: AsyncContract | None = None

    async def async_init(self) -> None:
        await self._init_contract_addresses()

    async def flush(self) -> None:
        log.warning("FLUSHING RP CACHE")
        self.CONTRACT_CACHE.clear()
        self.ABI_CACHE.clear()
        self.ADDRESS_CACHE.clear()
        self.addresses.clear()
        await self._init_contract_addresses()

    async def _init_contract_addresses(self) -> None:
        manual_addresses = cfg.rocketpool.manual_addresses
        for name, address in manual_addresses.items():
            self.addresses[name] = w3.to_checksum_address(address)

        self._multicall = await self.get_contract_by_name("multicall3")

        log.info("Indexing Rocket Pool contracts...")
        for path in Path("contracts/rocketpool/contracts/contract").rglob("*.sol"):
            file_name = path.stem
            contract = file_name[0].lower() + file_name[1:]
            try:
                await self.get_address_by_name(contract)
            except Exception:
                log.warning(f"Skipping {contract} in function list generation")
                continue

        try:
            cs_dir, cs_prefix = "ConstellationDirectory", "Constellation"
            self.addresses.update(
                {
                    f"{cs_prefix}.SuperNodeAccount": await self.call(
                        f"{cs_dir}.getSuperNodeAddress"
                    ),
                    f"{cs_prefix}.OperatorDistributor": await self.call(
                        f"{cs_dir}.getOperatorDistributorAddress"
                    ),
                    f"{cs_prefix}.Whitelist": await self.call(
                        f"{cs_dir}.getWhitelistAddress"
                    ),
                    f"{cs_prefix}.ETHVault": await self.call(
                        f"{cs_dir}.getWETHVaultAddress"
                    ),
                    f"{cs_prefix}.RPLVault": await self.call(
                        f"{cs_dir}.getRPLVaultAddress"
                    ),
                    "WETH": await self.call(f"{cs_dir}.getWETHAddress"),
                }
            )
        except NoAddressFound:
            log.warning("Failed to find address for Constellation contracts")

    @staticmethod
    def _abi_type_str(output: dict[str, Any]) -> str:
        """Convert a single ABI output entry to an eth_abi type string, handling tuples."""
        t: str = output["type"]
        if "tuple" in t:
            inner = ",".join(RocketPool._abi_type_str(c) for c in output["components"])
            suffix = t[5:]  # captures "", "[]", "[N]", etc.
            return f"({inner}){suffix}"
        return t

    @staticmethod
    def _decode_fn_output(fn: AsyncContractFunction, data: bytes) -> Any:
        """Decode raw ABI output bytes for a ContractFunction."""
        outputs = fn.abi["outputs"]
        if not outputs:
            return None
        types = [RocketPool._abi_type_str(dict(o)) for o in outputs]
        decoded = abi.decode(types, data)
        return decoded[0] if len(decoded) == 1 else decoded

    CallInput = AsyncContractFunction | tuple[AsyncContractFunction, bool]

    @staticmethod
    def _normalize_calls(
        calls: Sequence[CallInput], default_require_success: bool
    ) -> tuple[list[AsyncContractFunction], list[bool]]:
        """Normalize calls to (fn, allow_failure) pairs. Each call may be a
        plain AsyncContractFunction or an (fn, require_success) tuple."""
        fns: list[AsyncContractFunction] = []
        flags: list[bool] = []
        for call in calls:
            if isinstance(call, tuple):
                fn, req = call
            else:
                fn, req = call, default_require_success
            fns.append(fn)
            flags.append(not req)
        return fns, flags

    async def multicall(
        self,
        calls: Sequence[CallInput],
        require_success: bool = True,
        block: BlockIdentifier = "latest",
    ) -> list[Any]:
        """Multicall accepting AsyncContractFunction objects or (fn, require_success) tuples."""
        if not calls:
            return []

        fns, flags = self._normalize_calls(calls, require_success)
        encoded = [
            (fn.address, af, fn._encode_transaction_data())
            for fn, af in zip(fns, flags, strict=False)
        ]
        assert self._multicall is not None
        results = await self._multicall.functions.aggregate3(encoded).call(
            block_identifier=block
        )
        return [
            RocketPool._decode_fn_output(fns[i], data) if success else None
            for i, (success, data) in enumerate(results)
        ]

    async def get_address_by_name(self, name: str) -> ChecksumAddress:
        if name in self.ADDRESS_CACHE:
            return self.ADDRESS_CACHE[name]
        if name in self.addresses:
            self.ADDRESS_CACHE[name] = self.addresses[name]
            return self.addresses[name]
        address = await self.uncached_get_address_by_name(name)
        self.ADDRESS_CACHE[name] = address
        return address

    async def uncached_get_address_by_name(
        self, name: str, block: BlockIdentifier = "latest"
    ) -> ChecksumAddress:
        log.debug(f"Retrieving address for {name} Contract")
        sha3 = w3.solidity_keccak(["string", "string"], ["contract.address", name])
        storage = await self.get_contract_by_name(
            "rocketStorage", historical=block != "latest"
        )
        address: ChecksumAddress = await storage.functions.getAddress(sha3).call(
            block_identifier=block
        )
        if address == ADDRESS_ZERO:
            raise NoAddressFound(f"No address found for {name} Contract")
        self.addresses[name] = address
        log.debug(f"Retrieved address for {name} Contract: {address}")
        return address

    @staticmethod
    async def get_revert_reason(tnx: TxData) -> str:
        try:
            await w3.eth.call(
                {
                    "from": tnx["from"],
                    "to": tnx["to"],
                    "data": tnx["input"],
                    "gas": tnx["gas"],
                    "gasPrice": tnx["gasPrice"],
                    "value": tnx["value"],
                },
                block_identifier=tnx["blockNumber"],
            )
        except ContractLogicError as err:
            log.debug(f"Transaction: {tnx['hash']!r} ContractLogicError: {err}")
            return ", ".join(err.args)
        except ValueError as err:
            log.debug(f"Transaction: {tnx['hash']!r} ValueError: {err}")
            match err.args[0]["code"]:
                case -32000:
                    return "Out of gas"
                case _:
                    return "Hidden Error"
        else:
            return "Unknown"

    async def get_string(self, key: str) -> str:
        sha3 = w3.solidity_keccak(["string"], [key])
        storage = await self.get_contract_by_name("rocketStorage")
        return str(await storage.functions.getString(sha3).call())

    async def get_uint(self, key: str) -> int:
        sha3 = w3.solidity_keccak(["string"], [key])
        storage = await self.get_contract_by_name("rocketStorage")
        return int(await storage.functions.getUint(sha3).call())

    async def get_protocol_version(self) -> tuple:
        version_string = await self.get_string("protocol.version")
        return tuple(map(int, version_string.split(".")))

    async def get_abi_by_name(self, name: str) -> str:
        if name in self.ABI_CACHE:
            return self.ABI_CACHE[name]
        abi = await self.uncached_get_abi_by_name(name)
        self.ABI_CACHE[name] = abi
        return abi

    async def uncached_get_abi_by_name(self, name: str) -> str:
        log.debug(f"Retrieving abi for {name} contract")
        sha3 = w3.solidity_keccak(["string", "string"], ["contract.abi", name])
        storage = await self.get_contract_by_name("rocketStorage")
        compressed_string = await storage.functions.getString(sha3).call()
        if not compressed_string:
            raise Exception(f"No abi found for {name} contract")
        return str(decode_abi(compressed_string))

    async def assemble_contract(
        self,
        name: str,
        address: ChecksumAddress | None = None,
        historical: bool = False,
        mainnet: bool = False,
    ) -> AsyncContract:
        cache_key = (name, address, historical, mainnet)
        if cache_key in self.CONTRACT_CACHE:
            return self.CONTRACT_CACHE[cache_key]

        if name.startswith("Constellation."):
            short_name = name.removeprefix("Constellation.")
            abi_path = f"./contracts/constellation/{short_name}.abi.json"
        else:
            abi_path = f"./contracts/{name}.abi.json"

        if os.path.exists(abi_path):
            with open(abi_path) as f:
                abi = f.read()
        else:
            abi = await self.get_abi_by_name(name)

        if mainnet:
            contract = w3_mainnet.eth.contract(address=address, abi=abi)
        elif historical:
            contract = w3_archive.eth.contract(address=address, abi=abi)
        else:
            contract = w3.eth.contract(address=address, abi=abi)

        contract = cast(AsyncContract, contract)
        self.CONTRACT_CACHE[cache_key] = contract
        return contract

    def get_name_by_address(self, address: ChecksumAddress) -> str | None:
        return self.addresses.inverse.get(address, None)

    async def get_contract_by_name(
        self, name: str, historical: bool = False, mainnet: bool = False
    ) -> AsyncContract:
        address = await self.get_address_by_name(name)
        return await self.assemble_contract(
            name, address, historical=historical, mainnet=mainnet
        )

    async def get_contract_by_address(
        self, address: ChecksumAddress
    ) -> AsyncContract | None:
        """
        **WARNING**: only call after contract has been previously retrieved using its name
        """
        if not (name := self.get_name_by_address(address)):
            return None
        return await self.assemble_contract(name, address)

    async def estimate_gas_for_call(
        self, path: str, *args: Any, block: BlockIdentifier = "latest"
    ) -> int:
        log.debug(f"Estimating gas for {path} (block={block!r})")
        name, function = path.rsplit(".", 1)
        contract = await self.get_contract_by_name(name)
        return await contract.functions[function](*args).estimate_gas(
            {"gas": 2**32}, block_identifier=block
        )

    async def get_function(
        self,
        path: str,
        *args: Any,
        historical: bool = False,
        address: ChecksumAddress | None = None,
        mainnet: bool = False,
    ) -> AsyncContractFunction:
        name, function = path.rsplit(".", 1)
        if not address:
            address = await self.get_address_by_name(name)
        contract = await self.assemble_contract(name, address, historical, mainnet)
        args = tuple(
            w3.to_checksum_address(a) if isinstance(a, str) and w3.is_address(a) else a
            for a in args
        )
        return contract.functions[function](*args)

    async def call(
        self,
        path: str,
        *args: Any,
        block: BlockIdentifier = "latest",
        address: ChecksumAddress | None = None,
        mainnet: bool = False,
    ) -> Any:
        log.debug(f"Calling {path} (block={block!r})")
        fn = await self.get_function(
            path, *args, historical=block != "latest", address=address, mainnet=mainnet
        )
        return await fn.call(block_identifier=block)

    async def get_annual_rpl_inflation(self) -> float:
        inflation_per_interval: float = solidity.to_float(
            await self.call("rocketTokenRPL.getInflationIntervalRate")
        )
        if not inflation_per_interval:
            return 0
        seconds_per_interval: int = await self.call(
            "rocketTokenRPL.getInflationIntervalTime"
        )
        intervals_per_year = solidity.years / seconds_per_interval
        return float((inflation_per_interval**intervals_per_year) - 1.0)

    async def is_node(self, address: ChecksumAddress) -> bool:
        return bool(await self.call("rocketNodeManager.getNodeExists", address))

    async def is_minipool(self, address: ChecksumAddress) -> bool:
        return bool(await self.call("rocketMinipoolManager.getMinipoolExists", address))

    async def is_megapool(self, address: ChecksumAddress) -> bool:
        sha3 = w3.solidity_keccak(["string", "address"], ["megapool.exists", address])
        storage = await self.get_contract_by_name("rocketStorage")
        return bool(await storage.functions.getBool(sha3).call())

    async def get_eth_usdc_price(self) -> float:
        from rocketwatch.utils.liquidity import UniswapV3

        pool_address = await self.get_address_by_name("UniV3_USDC_ETH")
        pool = await UniswapV3.Pool.create(pool_address)
        return float(1.0 / await pool.get_normalized_price())

    async def get_reth_eth_price(self) -> float:
        from rocketwatch.utils.liquidity import UniswapV3

        pool_address = await self.get_address_by_name("UniV3_rETH_ETH")
        pool = await UniswapV3.Pool.create(pool_address)
        return float(await pool.get_normalized_price())


rp = RocketPool()
