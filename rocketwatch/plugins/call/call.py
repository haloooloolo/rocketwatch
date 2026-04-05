import contextlib
import json
import logging
from collections.abc import Sequence
from typing import Any, cast

import humanize
from discord import Interaction
from discord.app_commands import Choice, command, describe
from discord.ext.commands import Cog
from discord.ui import Modal, TextInput
from eth_typing import ABIComponent, BlockIdentifier, BlockNumber, ChecksumAddress
from web3.contract import AsyncContract

from rocketwatch.bot import RocketWatch
from rocketwatch.utils import solidity
from rocketwatch.utils.file import TextFile
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.shared_w3 import w3
from rocketwatch.utils.visibility import is_hidden_role_controlled

log = logging.getLogger("rocketwatch.call")


class CallModal(Modal):
    def __init__(
        self,
        cog: "Call",
        function: str,
        block: BlockIdentifier,
        address: ChecksumAddress | None,
        raw_output: bool,
        abi_inputs: Sequence[ABIComponent],
    ) -> None:
        func_name = function.rsplit(".", 1)[1] if "." in function else function
        super().__init__(title=func_name[:45])
        self.cog = cog
        self.function = function
        self.block: BlockIdentifier = block
        self.address = address
        self.raw_output = raw_output
        self.abi_inputs = abi_inputs
        self.param_inputs: list[TextInput[CallModal]] = []
        for inp in abi_inputs:
            text_input: TextInput[CallModal] = TextInput(
                label=f"{inp.get('name', '?')} ({inp['type']})"[:45], required=True
            )
            self.add_item(text_input)
            self.param_inputs.append(text_input)

    async def on_submit(self, interaction: Interaction) -> None:
        await interaction.response.defer(
            ephemeral=is_hidden_role_controlled(interaction)
        )
        args = []
        errors = []
        for text_input, inp in zip(self.param_inputs, self.abi_inputs, strict=True):
            val = text_input.value
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                val = json.loads(val)
            error = self._validate(val, inp["type"])
            if error:
                errors.append(f"`{inp.get('name', '?')}`: {error}")
            else:
                args.append(val)
        if errors:
            await interaction.followup.send(
                content="Validation failed:\n" + "\n".join(errors)
            )
            return
        await self.cog._execute_call(
            interaction, self.function, args, self.block, self.address, self.raw_output
        )

    @staticmethod
    def _validate(value: Any, abi_type: str) -> str | None:
        if abi_type == "bool":
            if not isinstance(value, bool):
                return f"expected bool, got `{value!r}`"
        elif abi_type == "address":
            if not isinstance(value, str) or not w3.is_address(value):
                return f"expected address, got `{value!r}`"
        elif abi_type == "string":
            if not isinstance(value, str):
                return f"expected string, got `{value!r}`"
        elif abi_type.startswith("uint") or abi_type.startswith("int"):
            if not isinstance(value, int) or isinstance(value, bool):
                return f"expected integer, got `{value!r}`"
        elif abi_type.startswith("bytes"):
            if isinstance(value, str):
                if not value.startswith("0x"):
                    return f"expected hex bytes, got `{value!r}`"
            elif not isinstance(value, (bytes, list)):
                return f"expected bytes, got `{value!r}`"
        return None


class Call(Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.function_names: list[str] = []

    @Cog.listener()
    async def on_ready(self) -> None:
        if self.function_names:
            return

        for contract_name in rp.addresses.copy():
            try:
                contract: AsyncContract = await rp.get_contract_by_name(contract_name)
                for entry in cast(list[dict[str, Any]], contract.abi):
                    if (
                        entry.get("type") == "function"
                        and "name" in entry
                        and entry.get("stateMutability") in ("view", "pure")
                    ):
                        func_id = f"{entry['name']}({','.join(inp['type'] for inp in entry.get('inputs', []))})"
                        self.function_names.append(f"{contract_name}.{func_id}")
            except Exception:
                log.exception(f"Could not get function list for {contract_name}")

    @command()
    @describe(block="call against block state")
    async def call(
        self,
        interaction: Interaction,
        function: str,
        block: str = "latest",
        address: str | None = None,
        raw_output: bool = False,
    ) -> None:
        """Manually call a function on a protocol contract"""

        block_id: BlockIdentifier
        if block.isnumeric():
            block_id = BlockNumber(int(block))
        elif block in ("earliest", "finalized", "safe", "latest"):
            block_id = cast(BlockIdentifier, block)
        else:
            await interaction.response.send_message("Invalid block identifier.")
            return

        verified_address: ChecksumAddress | None = None
        if address is not None:
            if w3.is_address(address):
                verified_address = w3.to_checksum_address(address)
            else:
                await interaction.response.send_message("Invalid contract address.")
                return

        # Look up ABI inputs for the function
        abi_inputs: Sequence[ABIComponent] = []
        try:
            contract_name, func_id = function.rsplit(".", 1)
            contract = await rp.get_contract_by_name(contract_name)
            for entry in cast(list[dict[str, Any]], contract.abi):
                if entry.get("type") == "function" and "name" in entry:
                    entry_id = f"{entry['name']}({','.join(inp['type'] for inp in entry.get('inputs', []))})"
                    if entry_id == func_id:
                        abi_inputs = entry.get("inputs", [])
                        break
        except Exception:
            pass

        if abi_inputs:
            modal = CallModal(
                self, function, block_id, verified_address, raw_output, abi_inputs
            )
            await interaction.response.send_modal(modal)
        else:
            await interaction.response.defer(
                ephemeral=is_hidden_role_controlled(interaction)
            )
            await self._execute_call(
                interaction, function, [], block_id, address, raw_output
            )

    async def _execute_call(
        self,
        interaction: Interaction,
        function: str,
        args: list[Any],
        block: BlockIdentifier,
        address: str | None,
        raw_output: bool,
    ) -> None:
        try:
            result = await rp.call(
                function,
                *args,
                block=block,
                address=w3.to_checksum_address(address) if address else None,
            )
        except Exception as err:
            await interaction.followup.send(content=f"Exception: ```{err!r}```")
            return
        gas_estimate: str
        try:
            gas_estimate = humanize.intcomma(
                await rp.estimate_gas_for_call(function, *args, block=block)
            )
        except Exception as err:
            gas_estimate = "N/A"
            if (
                isinstance(err, ValueError)
                and err.args
                and "code" in err.args
                and err.args[0]["code"] == -32000
            ):
                gas_estimate += f" ({err.args[0]['message']})"

        if isinstance(result, int) and abs(result) >= 10**12 and not raw_output:
            result = solidity.to_float(result)
        func_name = function.split("(")[0]
        text = f"`block: {block!s}`\n`gas estimate: {gas_estimate}`\n`{func_name}({', '.join([repr(a) for a in args])}): "
        if len(text + str(result)) > 2000:
            text += "too long, attached as file`"
            await interaction.followup.send(
                text, file=TextFile(str(result), "exception.txt")
            )
        else:
            text += f"{result!s}`"
            await interaction.followup.send(content=text)

    @call.autocomplete("function")
    async def match_function_name(
        self, interaction: Interaction, current: str
    ) -> list[Choice[str]]:
        return [
            Choice(name=name, value=name)
            for name in self.function_names
            if current.lower() in name.lower()
        ][:25]


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(Call(bot))
