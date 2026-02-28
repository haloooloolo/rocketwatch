import logging

from discord.ext import commands
from discord.ext.commands import hybrid_command, Context
from pymongo import AsyncMongoClient

from rocketwatch import RocketWatch
from utils.cfg import cfg
from utils.embeds import Embed, el_explorer_url
from utils.readable import render_tree_legacy
from utils.shared_w3 import w3
from utils.visibility import is_hidden_weak

log = logging.getLogger("beacon_states")
log.setLevel(cfg["log_level"])


class MinipoolStates(commands.Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot
        self.db = AsyncMongoClient(cfg["mongodb.uri"]).get_database("rocketwatch")

    @hybrid_command()
    async def minipool_states(self, ctx: Context):
        """Show minipool counts by beacon chain and contract status"""
        await ctx.defer(ephemeral=is_hidden_weak(ctx))
        # fetch from db
        res = await self.db.minipools.find({
            "beacon.status": {"$exists": True}
        }).to_list(None)
        data = {
            "pending": {},
            "active" : {},
            "exiting": {},
            "exited" : {},
            "withdrawn": {},
            "closed": {}
        }
        exiting_valis = []
        withdrawn_valis = []
        for minipool in res:
            match minipool["beacon"]["status"]:
                case "pending_initialized":
                    data["pending"]["initialized"] = data["pending"].get("initialized", 0) + 1
                case "pending_queued":
                    data["pending"]["queued"] = data["pending"].get("queued", 0) + 1
                case "active_ongoing":
                    data["active"]["ongoing"] = data["active"].get("ongoing", 0) + 1
                case "active_exiting":
                    data["exiting"]["voluntarily"] = data["exiting"].get("voluntarily", 0) + 1
                    exiting_valis.append(minipool)
                case "active_slashed":
                    data["exiting"]["slashed"] = data["exiting"].get("slashed", 0) + 1
                    exiting_valis.append(minipool)
                case "exited_unslashed" | "exited_slashed" | "withdrawal_possible":
                    status_2 = "slashed" if minipool["beacon"]["slashed"] else "voluntarily" 
                    data["exited"][status_2] = data["exited"].get(status_2, 0) + 1
                    exiting_valis.append(minipool)
                case "withdrawal_done":
                    status_2 = "slashed" if minipool["beacon"]["slashed"] else "unslashed" 
                    if not minipool["finalized"]:
                        data["withdrawn"][status_2] = data["withdrawn"].get(status_2, 0) + 1
                        withdrawn_valis.append(minipool)
                    else:
                        data["closed"][status_2] = data["closed"].get(status_2, 0) + 1
                case _:
                    logging.warning(f"Unknown status {minipool['status']}")

        embed = Embed(title="Minipool States", color=0x00ff00)
        description = "```\n"
        # render dict as a tree like structure
        description += render_tree_legacy(data, "Minipools")
        
        total_listed_valis = len(exiting_valis) + len(withdrawn_valis)

        if total_listed_valis == 0:
            description += "```"
        elif total_listed_valis < 24:
            description += "\n"
            if len(exiting_valis) > 0:
                description += "\n--- Exiting Minipools ---\n\n"
                valis = sorted([v["validator_index"] for v in exiting_valis])
                description += ", ".join([str(v) for v in valis])
            if len(withdrawn_valis) > 0:
                description += "\n--- Withdrawn Minipools ---\n\n"
                valis = sorted([v["validator_index"] for v in withdrawn_valis])
                description += ", ".join([str(v) for v in valis])
            description += "```"
        else:
            description += "```"
            
            node_operators = []            
            for valis in (exiting_valis, withdrawn_valis):
                valis_no = {}
                # dedupe, add count of validators with matching node operator
                for v in valis:
                    valis_no[v["node_operator"]] = valis_no.get(v["node_operator"], 0) + 1
                # turn into list
                valis_no = list(valis_no.items())
                # sort by count
                valis_no.sort(key=lambda x: x[1], reverse=True)
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
        await ctx.send(embed=embed)


async def setup(self):
    await self.add_cog(MinipoolStates(self))
