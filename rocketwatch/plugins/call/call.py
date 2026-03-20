import contextlib
import json
import logging

import humanize
from discord import Interaction
from discord.app_commands import Choice, command, describe
from discord.ext.commands import Cog
from discord.ui import Modal, TextInput

from rocketwatch import RocketWatch
from utils import solidity
from utils.file import TextFile
from utils.rocketpool import rp
from utils.shared_w3 import w3
from utils.visibility import is_hidden_role_controlled

log = logging.getLogger("rocketwatch.call")


class CallModal(Modal):
    def __init__(self, cog, function, block, address, raw_output, abi_inputs):
        func_name = function.rsplit(".", 1)[1] if "." in function else function
        super().__init__(title=func_name[:45])
        self.cog = cog
        self.function = function
        self.block = block
        self.address = address
        self.raw_output = raw_output
        self.abi_inputs = abi_inputs
        self.param_inputs: list[TextInput] = []
        for inp in abi_inputs:
            text_input: TextInput = TextInput(
                label=f"{inp['name']} ({inp['type']})"[:45], required=True
            )
            self.add_item(text_input)
            self.param_inputs.append(text_input)

    async def on_submit(self, interaction):
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
                errors.append(f"`{inp['name']}`: {error}")
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
    def _validate(value, abi_type):
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
    async def on_ready(self):
        if self.function_names:
            return

        for contract in rp.addresses.copy():
            try:
                c = await rp.get_contract_by_name(contract)
                for entry in c.abi:
                    if (
                        entry.get("type") == "function"
                        and "name" in entry
                        and entry.get("stateMutability") in ("view", "pure")
                    ):
                        func_id = f"{entry['name']}({','.join(inp['type'] for inp in entry.get('inputs', []))})"
                        self.function_names.append(f"{contract}.{func_id}")
            except Exception:
                log.exception(f"Could not get function list for {contract}")

    @command()
    @describe(block="call against block state")
    async def call(
        self,
        interaction: Interaction,
        function: str,
        block: str = "latest",
        address: str | None = None,
        raw_output: bool = False,
    ):
        """Manually call a function on a protocol contract"""
        block_id: int | str = int(block) if block.isnumeric() else block

        # Look up ABI inputs for the function
        abi_inputs = []
        try:
            contract_name, func_id = function.rsplit(".", 1)
            contract = await rp.get_contract_by_name(contract_name)
            for entry in contract.abi:
                if entry.get("type") == "function" and "name" in entry:
                    entry_id = f"{entry['name']}({','.join(inp['type'] for inp in entry.get('inputs', []))})"
                    if entry_id == func_id:
                        abi_inputs = entry.get("inputs", [])
                        break
        except Exception:
            pass

        if abi_inputs:
            modal = CallModal(self, function, block_id, address, raw_output, abi_inputs)
            await interaction.response.send_modal(modal)
        else:
            await interaction.response.defer(
                ephemeral=is_hidden_role_controlled(interaction)
            )
            await self._execute_call(
                interaction, function, [], block_id, address, raw_output
            )

    async def _execute_call(
        self, interaction, function, args, block, address, raw_output
    ):
        try:
            v = await rp.call(
                function,
                *args,
                block=block,
                address=w3.to_checksum_address(address) if address else None,
            )
        except Exception as err:
            await interaction.followup.send(content=f"Exception: ```{err!r}```")
            return
        try:
            g = await rp.estimate_gas_for_call(function, *args, block=block)
        except Exception as err:
            g = "N/A"
            if (
                isinstance(err, ValueError)
                and err.args
                and "code" in err.args
                and err.args[0]["code"] == -32000
            ):
                g += f" ({err.args[0]['message']})"

        if isinstance(v, int) and abs(v) >= 10**12 and not raw_output:
            v = solidity.to_float(v)
        g = humanize.intcomma(g)
        func_name = function.split("(")[0]
        text = f"`block: {block}`\n`gas estimate: {g}`\n`{func_name}({', '.join([repr(a) for a in args])}): "
        if len(text + str(v)) > 2000:
            text += "too long, attached as file`"
            await interaction.followup.send(
                text, file=TextFile(str(v), "exception.txt")
            )
        else:
            text += f"{v!s}`"
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


async def setup(bot):
    await bot.add_cog(Call(bot))
