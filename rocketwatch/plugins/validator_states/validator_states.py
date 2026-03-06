import logging

from discord import Interaction
from discord.ext import commands
from discord.app_commands import command

from rocketwatch import RocketWatch
from utils.cfg import cfg
from utils.embeds import Embed, el_explorer_url
from utils.readable import render_tree_legacy
from utils.shared_w3 import w3
from utils.visibility import is_hidden_weak

log = logging.getLogger("validator_states")
log.setLevel(cfg["log_level"])


_BEACON_PENDING = {"in_queue": "unassigned", "prestaked": "prestaked", "staking": "staked"}

def _classify_beacon_validator(beacon, contract_status):
    """Classify a validator by beacon status. Returns (status, sub_status)."""
    match beacon["status"]:
        case "pending_initialized":
            if contract_status == "dissolved":
                return "dissolved", None
            else:
                return "pending", _BEACON_PENDING[contract_status]
        case "pending_queued":
            return "pending", "queued"
        case "active_ongoing":
            return "active", "ongoing"
        case "active_exiting":
            return "exiting", "voluntarily"
        case "active_slashed":
            return "exiting", "slashed"
        case "exited_unslashed" | "exited_slashed" | "withdrawal_possible":
            sub = "slashed" if beacon["slashed"] else "voluntarily"
            return "exited", sub
        case "withdrawal_done":
            sub = "slashed" if beacon["slashed"] else "unslashed"
            return "withdrawn", sub
        case _:
            log.warning(f"Unknown beacon status {beacon['status']}")
            return None, None


def _empty_state_tree():
    return {
        "dissolved": 0,
        "pending": {},
        "active": {},
        "exiting": {},
        "exited": {},
        "withdrawn": {},
        "closed": {}
    }

def _classify_collection(docs, done_fn):
    """Classify docs into state tree.

    Args:
        docs: list of DB documents, with or without beacon data
        done_fn: function that takes a doc and returns True if its lifecycle is complete
                 (used to distinguish withdrawn vs closed for withdrawal_done validators)
    """
    data = _empty_state_tree()
    exiting_valis = []
    withdrawn_valis = []

    for doc in docs:
        beacon = doc.get("beacon")
        contract_status = doc.get("status", "")

        if beacon is None:
            sub = _BEACON_PENDING.get(contract_status)
            if sub:
                data["pending"][sub] = data["pending"].get(sub, 0) + 1
            elif contract_status == "dissolved":
                data["dissolved"] += 1
            continue

        category, sub = _classify_beacon_validator(beacon, contract_status)
        if category is None:
            continue
        if category == "withdrawn" and done_fn(doc):
            category = "closed"
        if category == "dissolved":
            data["dissolved"] += 1
        else:
            data[category][sub] = data[category].get(sub, 0) + 1
        if category in ("exiting", "exited"):
            exiting_valis.append(doc)
        elif category == "withdrawn":
            withdrawn_valis.append(doc)

    return data, exiting_valis, withdrawn_valis


def _collapse_tree(data: dict) -> dict:
    collapsed_data = {}
    for status in data.keys():
        if isinstance(data[status], dict) and len(data[status]) == 1:
            sub_status = list(data[status].keys())[0]
            collapsed_data[status] = data[status][sub_status]
        else:
            collapsed_data[status] = data[status]
    return collapsed_data


class ValidatorStates(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    @command()
    async def validator_states(self, interaction: Interaction):
        """Show validator counts by beacon chain and contract status"""
        await interaction.response.defer(ephemeral=is_hidden_weak(interaction))

        minipools = await self.bot.db.minipools.find(
            {"beacon.status": {"$exists": True}},
            {"beacon": 1, "status": 1, "finalized": 1, "node_operator": 1, "validator_index": 1}
        ).to_list(None)
        megapool_vals = await self.bot.db.megapool_validators.find(
            {}, {"beacon": 1, "status": 1, "node_operator": 1, "validator_index": 1}
        ).to_list(None)

        mp_data, mp_exiting, mp_withdrawn = _classify_collection(
            minipools, lambda d: d.get("finalized", False)
        )
        mg_data, mg_exiting, mg_withdrawn = _classify_collection(
            megapool_vals, lambda d: d.get("status") == "exited"
        )
        
        tree = {
            "minipools": _collapse_tree(mp_data),
            "megapools": _collapse_tree(mg_data),
        }

        embed = Embed(title="Validator States", color=0x00ff00)
        description = "```\n"
        description += render_tree_legacy(tree, "Validators")

        exiting_valis = mp_exiting + mg_exiting
        withdrawn_valis = mp_withdrawn + mg_withdrawn
        total_listed_valis = len(exiting_valis) + len(withdrawn_valis)

        if total_listed_valis == 0:
            description += "```"
        elif total_listed_valis < 24:
            description += "\n"
            if exiting_valis:
                description += "\n--- Exiting Validators ---\n\n"
                valis = sorted([v["validator_index"] for v in exiting_valis])
                description += ", ".join([str(v) for v in valis])
            if withdrawn_valis:
                description += "\n--- Withdrawn Validators ---\n\n"
                valis = sorted([v["validator_index"] for v in withdrawn_valis])
                description += ", ".join([str(v) for v in valis])
            description += "```"
        else:
            description += "```"

            node_operators = []
            for valis in (exiting_valis, withdrawn_valis):
                valis_no = {}
                for v in valis:
                    no = v["node_operator"]
                    valis_no[no] = valis_no.get(no, 0) + 1
                valis_no = sorted(valis_no.items(), key=lambda x: x[1], reverse=True)
                node_operators.append(valis_no)

            exiting_node_operators, withdrawn_node_operators = node_operators
            max_total_list_length = 16

            if len(exiting_node_operators) + len(withdrawn_node_operators) <= max_total_list_length:
                num_exiting = len(exiting_node_operators)
                num_withdrawn = len(withdrawn_node_operators)
            elif len(exiting_node_operators) >= len(withdrawn_node_operators):
                num_withdrawn = min(len(withdrawn_node_operators), max_total_list_length // 2)
                num_exiting = max_total_list_length - num_withdrawn
            else:
                num_exiting = min(len(exiting_node_operators), max_total_list_length // 2)
                num_withdrawn = max_total_list_length - num_exiting

            if num_exiting > 0:
                description += "\n**Exiting Node Operators**\n"
                description += ", ".join([f"{el_explorer_url(w3.to_checksum_address(v))} ({c})" for v, c in exiting_node_operators[:num_exiting]])
                if remaining_no := exiting_node_operators[num_exiting:]:
                    num_remaining_valis = sum([c for _, c in remaining_no])
                    description += f", and {len(remaining_no)} more ({num_remaining_valis})"
                description += "\n"
            if num_withdrawn > 0:
                description += "\n**Withdrawn Node Operators**\n"
                description += ", ".join([f"{el_explorer_url(w3.to_checksum_address(v))} ({c})" for v, c in withdrawn_node_operators[:num_withdrawn]])
                if remaining_no := withdrawn_node_operators[num_withdrawn:]:
                    num_remaining_valis = sum([c for _, c in remaining_no])
                    description += f", and {len(remaining_no)} more ({num_remaining_valis})"
                description += "\n"

        embed.description = description
        await interaction.followup.send(embed=embed)


async def setup(self):
    await self.add_cog(ValidatorStates(self))
