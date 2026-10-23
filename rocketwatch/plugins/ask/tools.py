import asyncio
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import aiohttp
import numpy as np
from bson import json_util
from eth_typing import BlockIdentifier, BlockNumber
from hexbytes import HexBytes
from matplotlib.figure import Figure
from pydantic import BaseModel, Field
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase
from web3.exceptions import ContractLogicError
from web3.types import RPCEndpoint

from rocketwatch.plugins.collateral.collateral import get_node_collateral_data
from rocketwatch.plugins.signaling.signaling import Proposal, Signaling
from rocketwatch.utils import solidity
from rocketwatch.utils.address_labels import get_address_name
from rocketwatch.utils.agent import AgentTool, ToolError, called_from_code
from rocketwatch.utils.block_time import block_to_ts, ts_to_block
from rocketwatch.utils.charts import render_png
from rocketwatch.utils.config import cfg
from rocketwatch.utils.ens import get_name, resolve_name
from rocketwatch.utils.event_logs import get_logs
from rocketwatch.utils.rocketpool import NoAddressFound, RocketPool, rp
from rocketwatch.utils.shared_w3 import bacon, w3

if TYPE_CHECKING:
    from anthropic.types.beta import BetaToolUnionParam

MAX_RESULT_CHARS = 30_000
MAX_SCRIPT_RESULT_CHARS = 5_000_000
MAX_DOCS = 200
MAX_TIME_MS = 30_000
MAX_LOG_BLOCK_RANGE = 100_000
MAX_EVENT_BLOCK_RANGE = 1_000_000
MAX_MULTICALL = 500
MAX_FILES = 10
MAX_GITHUB_RESULTS = 30
GITHUB_ORG = "rocket-pool"
GITHUB_SEARCH_URL = "https://api.github.com/search/code"
GITHUB_TIMEOUT = aiohttp.ClientTimeout(total=20)

ROCKETDASH_API = "https://rocketdash.net/api/"
ROCKETDASH_TIMEOUT = aiohttp.ClientTimeout(total=30)
_ROCKETDASH_PATH = re.compile(r"^[a-z0-9-]+(/[a-z0-9-]+)*$")
_ROCKETDASH_BLOCKED = (
    "gov/admin",
    "gov/audit",
    "gov/delete-proposal",
    "gov/vote",
    "live/stream",
    "memories",
)
# from the rocketdash.net frontend; no published API docs
ROCKETDASH_ENDPOINTS = """\
beacon-queue/{current,history}
clients/{family-history,history,list,summary,window-summary}
deposit-pool/{events,history,summary}
eth-price
gov/{participation,proposal-detail,proposal-votes,proposals,status}
governance/{delegates,nodes,proposal-votes,proposals,snapshot/proposals,snapshot/proposal-detail,snapshot/proposal-votes,snapshot/space}
home
megapools/{detail,distribution,late-proofs,list,summary,validators}
minipools/{detail,list,live-attestations,summary}
network-health/{delta,epoch-current,history,summary}
nodes/{detail,history,list,live-attestations,performance,performance-map,summary,validators}
odao/{member-activity,overview,reward-tree}
proposals/{builders,leaderboard,list,missed,summary}
protocol-settings
queue/{current,history,summary,wait-time}
reth/{apr-history,dex-history,dex-trades,events,history,holders,rewards,slots,summary}
reth-flow/{connect,events,overview,search,summary,wallet}
rewards/{distribution,intervals,leaderboard,node,node-voter-share,search,summary,voter-share-snapshots}
rock-reth/{apr-history,history,holders,rewards,summary}
rock-reth-flow/{connect,events,overview,search,summary,wallet}
rpl/{events,history,holders,summary,top-stakers,unstaking}
rpl-flow/{connect,events,overview,search,summary,wallet}
sync-committee/summary
trends/{history,summary}
validators/{activity,detail,filtered-distribution,history,list,live-attestations,pressure,summary,transitions}"""

# run by Anthropic. The basic versions: the newer ones bring a code sandbox
# of their own, which would sit next to the code execution tool
WEB_TOOLS: "list[BetaToolUnionParam]" = [
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 8},
    {
        "type": "web_fetch_20250910",
        "name": "web_fetch",
        "max_uses": 10,
        "max_content_tokens": 10_000,
    },
]

DEFILLAMA_HOSTS = {
    "api": "https://api.llama.fi/",
    "yields": "https://yields.llama.fi/",
    "coins": "https://coins.llama.fi/",
    "stablecoins": "https://stablecoins.llama.fi/",
}
DEFILLAMA_TIMEOUT = aiohttp.ClientTimeout(total=60)
_DEFILLAMA_PATH = re.compile(r"^[A-Za-z0-9_.,:/-]+$")
SAMPLE_SIZE = 50

_WRITE_STAGES = {"$out", "$merge"}
_RPC_PREFIXES = ("eth_", "net_", "web3_", "debug_trace", "trace_", "txpool_")
_RPC_BLOCKED = ("eth_send", "eth_sign", "eth_submit")
_BLOCK_TAGS = {"latest", "safe", "finalized", "pending"}
_UNFILTERED_VALIDATORS = re.compile(
    r"^/eth/v\d+/beacon/states/[^/]+/(validators|validator_balances)/?$"
)
_FILE_EXTENSIONS = {"csv", "txt", "md", "json"}

# categorical slots in fixed order, chart chrome for a light surface
PALETTE = [
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
]
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _truncate(text: str) -> str:
    if called_from_code():
        # a script needs whole JSON, and its results don't fill the context
        if len(text) > MAX_SCRIPT_RESULT_CHARS:
            raise ToolError(
                f"Result is {len(text):,} chars, over the {MAX_SCRIPT_RESULT_CHARS:,} "
                "limit for scripts; narrow the query"
            )
        return text
    if len(text) <= MAX_RESULT_CHARS:
        return text
    cut = len(text) - MAX_RESULT_CHARS
    return f"{text[:MAX_RESULT_CHARS]}\n…[truncated {cut} chars, narrow the query]"


def _dump(value: Any) -> str:
    return _truncate(json.dumps(_jsonable(value), default=str))


def _dump_bson(value: Any) -> str:
    return _truncate(
        json_util.dumps(value, json_options=json_util.RELAXED_JSON_OPTIONS)
    )


def _from_ejson(value: Any) -> Any:
    """Decode Extended JSON ($date, $oid, ...) in model-supplied queries."""
    return json_util.loads(json.dumps(value))


def _check_read_only(value: Any) -> None:
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in _WRITE_STAGES:
                raise ToolError(f"{key} writes to the database and is not allowed")
            _check_read_only(inner)
    elif isinstance(value, list):
        for inner in value:
            _check_read_only(inner)


def _bson_type(value: Any) -> str:
    match value:
        case None:
            return "null"
        case bool():
            return "bool"
        case int():
            return "int"
        case float():
            return "double"
        case str():
            return "string"
        case datetime():
            return "date"
        case dict():
            return "object"
        case list():
            return "array"
        case _:
            return type(value).__name__


def _collect_fields(
    doc: Mapping[str, Any], prefix: str, fields: dict[str, dict[str, Any]]
) -> None:
    for key, value in doc.items():
        path = f"{prefix}{key}"
        entry = fields.setdefault(path, {"types": set(), "example": None})
        entry["types"].add(_bson_type(value))
        if isinstance(value, dict):
            _collect_fields(value, f"{path}.", fields)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            _collect_fields(value[0], f"{path}[].", fields)
        elif value is not None and entry["example"] is None:
            entry["example"] = json_util.dumps(value)[:80]


def _block_id(block: int | str) -> BlockIdentifier:
    if isinstance(block, int):
        return block
    if block.isdigit():
        return int(block)
    if block.startswith("0x"):
        return int(block, 16)
    if block in _BLOCK_TAGS or block == "earliest":
        return block  # type: ignore[return-value]
    raise ToolError(f"Invalid block {block!r}")


def _resolve_block(tag: Any, latest: int) -> int:
    if isinstance(tag, int):
        return tag
    if tag == "earliest":
        return 0
    if tag is None or tag in _BLOCK_TAGS:
        return latest
    tag = str(tag)
    try:
        return int(tag) if tag.isdigit() else int(tag, 16)
    except ValueError:
        raise ToolError(f"Invalid block {tag!r}") from None


def _coerce_arg(abi_type: str, value: Any) -> Any:
    if abi_type.endswith("]") and isinstance(value, list):
        inner = abi_type[: abi_type.rindex("[")]
        return [_coerce_arg(inner, v) for v in value]
    if not isinstance(value, str):
        return value
    if abi_type.startswith(("uint", "int")):
        return int(value, 0)
    if abi_type == "address":
        return w3.to_checksum_address(value)
    if abi_type.startswith("bytes"):
        return HexBytes(value)
    return value


def _signature(entry: Mapping[str, Any]) -> str:
    inputs = ", ".join(
        f"{RocketPool._abi_type_str(dict(i))} {i.get('name', '')}".rstrip()
        for i in entry.get("inputs", [])
    )
    outputs = ", ".join(
        f"{RocketPool._abi_type_str(dict(o))} {o.get('name', '')}".rstrip()
        for o in entry.get("outputs", [])
    )
    mutability = entry.get("stateMutability", "")
    return f"{entry['name']}({inputs}) {mutability} returns ({outputs})"


def _to_number(value: float | str) -> float:
    try:
        return float(value)
    except ValueError:
        raise ToolError(f"Expected a number, got {value!r}") from None


def _to_datetime(value: float | str) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return _to_datetime(_to_number(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.fromtimestamp(value, UTC)


class NoInput(BaseModel):
    pass


class CollectionInput(BaseModel):
    collection: str


class MongoFindInput(BaseModel):
    collection: str
    filter: dict[str, Any] = Field(
        default_factory=dict, description="MongoDB query filter (Extended JSON)"
    )
    projection: dict[str, Any] | None = None
    sort: dict[str, Literal[1, -1]] | None = Field(
        None, description="Ordered field -> 1 (asc) / -1 (desc)"
    )
    skip: int = Field(0, ge=0)
    limit: int = Field(20, ge=1, le=MAX_DOCS)


class MongoAggregateInput(BaseModel):
    collection: str
    pipeline: list[dict[str, Any]] = Field(
        description="Aggregation pipeline (Extended JSON); $out and $merge are rejected"
    )


class ContractInput(BaseModel):
    contract: str = Field(
        description="Rocket Pool contract name (e.g. rocketNodeManager) or a bundled ABI name (e.g. ERC20)"
    )
    address: str | None = Field(
        None,
        description="Required for bundled ABIs and per-instance contracts like rocketMegapoolDelegate; defaults to the registered address",
    )


class ContractCallSpec(ContractInput):
    function: str
    args: list[Any] = Field(
        default_factory=list,
        description="Positional arguments; pass large integers as decimal strings",
    )


class ContractCallInput(ContractCallSpec):
    block: int | str = Field(
        "latest", description="Block number or tag (latest, finalized, ...)"
    )


class ContractCallManyInput(BaseModel):
    calls: list[ContractCallSpec] = Field(min_length=1, max_length=MAX_MULTICALL)
    block: int | str = Field(
        "latest", description="Block number or tag (latest, finalized, ...)"
    )


class ContractEventsInput(ContractInput):
    event: str
    from_block: int | str = Field(description="Block number or tag")
    to_block: int | str = "latest"
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Indexed argument name -> value (or list of values) to match",
    )
    limit: int = Field(
        50, ge=1, le=MAX_DOCS, description="Return at most this many, newest first"
    )


class EthRpcInput(BaseModel):
    method: str = Field(description="JSON-RPC method, e.g. eth_getLogs")
    params: list[Any] = Field(default_factory=list)


class BeaconGetInput(BaseModel):
    path: str = Field(description="Beacon API path, e.g. /eth/v1/beacon/headers/head")
    params: dict[str, str] = Field(default_factory=dict)


class NodeCollateralInput(BaseModel):
    node_address: str | None = Field(
        None, description="A node address for its figures; omit for a network summary"
    )


class ChartSeries(BaseModel):
    label: str
    x: list[float | str]
    y: list[float]


class ChartInput(BaseModel):
    filename: str = Field(description="Base name without extension")
    kind: Literal["line", "bar", "scatter", "area"]
    title: str
    x_label: str = ""
    y_label: str = ""
    x_type: Literal["number", "category", "time"] = Field(
        "number",
        description="time: unix timestamps in seconds or ISO dates. Bars always treat x as categories",
    )
    series: list[ChartSeries] = Field(min_length=1, max_length=len(PALETTE))
    stacked: bool = Field(False, description="Stack bar or area series")
    log_y: bool = False


class FileInput(BaseModel):
    filename: str = Field(description="Name with extension: .csv, .txt, .md or .json")
    content: str


class BlockTimeInput(BaseModel):
    timestamp: int | str | None = Field(
        None, description="Unix seconds or an ISO date/time (UTC) to find the block for"
    )
    block: int | None = Field(None, ge=0, description="A block to find the time of")


class AddressInfoInput(BaseModel):
    address: str = Field(description="An address or an ENS name")


class RocketDashInput(BaseModel):
    endpoint: str = Field(description="Path after /api/, e.g. rewards/node")
    params: dict[str, str] = Field(
        default_factory=dict,
        description="Query parameters; network is added automatically",
    )


class SignalingProposalsInput(BaseModel):
    title: str | None = Field(
        None, description="Only proposals whose title contains this (case-insensitive)"
    )
    limit: int = Field(10, ge=1, le=MAX_DOCS, description="Newest first")


class SignalingVotesInput(BaseModel):
    proposal_id: str
    limit: int = Field(50, ge=1, le=MAX_DOCS, description="Largest voting power first")


def _choice_labels(choices: list[str], choice: Any) -> Any:
    # choices are 1-based indices into the proposal's choice list
    match choice:
        case int():
            return choices[choice - 1]
        case list():
            return [choices[c - 1] for c in choice]
        case dict():
            return {choices[int(c) - 1]: weight for c, weight in choice.items()}
        case _:
            return choice


def _proposal_summary(proposal: Proposal) -> dict[str, Any]:
    total = proposal.scores_total
    return {
        "id": proposal.id,
        "title": proposal.title,
        "vote_type": proposal.vote_type,
        "state": proposal.state,
        "start": proposal.start,
        "end": proposal.end,
        "results": [
            {
                "choice": choice,
                "power": score,
                "pct": score / total * 100 if total else None,
            }
            for choice, score in zip(proposal.choices, proposal.scores, strict=False)
        ],
        "total_power": total,
        "quorum": proposal.quorum,
        "reached_quorum": proposal.reached_quorum(),
        "url": proposal.url,
    }


class DefiLlamaInput(BaseModel):
    host: Literal["api", "yields", "coins", "stablecoins"]
    path: str = Field(
        description="e.g. protocol/rocket-pool, pools, prices/current/..."
    )
    params: dict[str, str] = Field(default_factory=dict)
    where: dict[str, str] = Field(
        default_factory=dict,
        description="Keep only list items whose fields equal these values "
        "(case-insensitive), e.g. {'project': 'lido', 'chain': 'Ethereum'}",
    )


class GithubSearchInput(BaseModel):
    query: str = Field(
        description="GitHub code search terms; qualifiers like repo:rocket-pool/smartnode, path:, filename: and language: work"
    )
    org: str | None = Field(
        GITHUB_ORG,
        description="Organization to search, e.g. rocket-pool or ethereum (EIPs, "
        "specs, clients); null searches all of GitHub",
    )
    limit: int = Field(10, ge=1, le=MAX_GITHUB_RESULTS)


def _where(data: Any, conditions: dict[str, str]) -> Any:
    """Filter the list in a response (top level or under "data") by field values."""
    if not conditions:
        return data

    def keep(item: Any) -> bool:
        return isinstance(item, dict) and all(
            str(item.get(k, "")).lower() == v.lower() for k, v in conditions.items()
        )

    if isinstance(data, list):
        return [item for item in data if keep(item)]
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return {**data, "data": [item for item in data["data"] if keep(item)]}
    raise ToolError("where only applies to responses that are a list of objects")


def _raw_url(html_url: str) -> str:
    # github.com/<owner>/<repo>/blob/<ref>/<path> -> raw.githubusercontent.com/...
    return html_url.replace(
        "https://github.com/", "https://raw.githubusercontent.com/", 1
    ).replace("/blob/", "/", 1)


class AskTools:
    """The read-only tools for one question, and the files it produces."""

    def __init__(
        self, db: AsyncDatabase[dict[str, Any]], github_token: str = ""
    ) -> None:
        self.db = db
        self.github_token = github_token
        self.files: dict[str, bytes] = {}

    def definitions(self) -> list[AgentTool]:
        tools = [
            AgentTool(
                "mongo_list_collections",
                "List the bot's MongoDB collections with document counts.",
                NoInput,
                self.mongo_list_collections,
            ),
            AgentTool(
                "mongo_describe_collection",
                "Show a collection's indexes and field paths with types and example "
                "values, sampled from its documents. Do this before querying a "
                "collection for the first time.",
                CollectionInput,
                self.mongo_describe_collection,
            ),
            AgentTool(
                "mongo_find",
                f"Find documents in a collection. Returns at most {MAX_DOCS}.",
                MongoFindInput,
                self.mongo_find,
            ),
            AgentTool(
                "mongo_aggregate",
                "Run an aggregation pipeline. Prefer this for counts, sums, "
                f"grouping and distinct values. Returns at most {MAX_DOCS} documents.",
                MongoAggregateInput,
                self.mongo_aggregate,
            ),
            AgentTool(
                "list_contracts",
                "List registered contract names and addresses, plus ABI names that "
                "need an explicit address.",
                NoInput,
                self.list_contracts,
            ),
            AgentTool(
                "contract_info",
                "Show a contract's address and the signatures of its functions and "
                "events.",
                ContractInput,
                self.contract_info,
            ),
            AgentTool(
                "contract_call",
                "Call a contract function via eth_call and return the decoded "
                "result with its output types. Integers are raw (usually wei). "
                "Any function can be simulated; nothing is ever sent on-chain.",
                ContractCallInput,
                self.contract_call,
            ),
            AgentTool(
                "contract_call_many",
                f"Make up to {MAX_MULTICALL} contract calls in one multicall, all "
                "at the same block, e.g. one function across many nodes or "
                "megapools. Results are in call order, null where a call reverted. "
                "Use this rather than many separate contract_calls.",
                ContractCallManyInput,
                self.contract_call_many,
            ),
            AgentTool(
                "contract_events",
                "Fetch a contract's decoded event logs by event name over a block "
                f"range of at most {MAX_EVENT_BLOCK_RANGE} blocks (~12s per block). "
                "Returns the total match count and the newest events.",
                ContractEventsInput,
                self.contract_events,
            ),
            AgentTool(
                "eth_rpc",
                "Send a read-only JSON-RPC request to the execution node, e.g. "
                "eth_getBalance, eth_getTransactionReceipt, eth_getLogs (at most "
                f"{MAX_LOG_BLOCK_RANGE} blocks per call), debug_traceTransaction.",
                EthRpcInput,
                self.eth_rpc,
            ),
            AgentTool(
                "beacon_get",
                "GET a beacon node API path. Validator state endpoints need an id "
                "filter (indices or pubkeys, comma-separated). Balances are in gwei.",
                BeaconGetInput,
                self.beacon_get,
            ),
            AgentTool(
                "node_collateral",
                "RPL collateral as computed by the bot's /collateral commands: bonded "
                "and borrowed ETH, RPL stake and RPL value as a percentage of each. "
                "For one node, or a network summary with percentiles.",
                NodeCollateralInput,
                self.node_collateral,
            ),
            AgentTool(
                "make_chart",
                "Render a PNG chart that can be attached to the answer. Returns its "
                "filename.",
                ChartInput,
                self.make_chart,
            ),
            AgentTool(
                "save_file",
                "Save a text file (e.g. a CSV of results) that can be attached to the "
                "answer. Returns its filename.",
                FileInput,
                self.save_file,
            ),
            AgentTool(
                "block_time",
                "Find the block closest to a time, or the time of a block. Use it to "
                "pick blocks for historical contract calls and event ranges; the node "
                "keeps full history.",
                BlockTimeInput,
                self.block_time,
            ),
            AgentTool(
                "address_info",
                "Identify an address or ENS name: its ENS name, the label the bot "
                "shows for it, whether it is a Rocket Pool contract, and whether it is "
                "a registered node, minipool or megapool.",
                AddressInfoInput,
                self.address_info,
            ),
            AgentTool(
                "rocketdash_get",
                "GET the rocketdash.net API, a Rocket Pool analytics site with "
                "history and aggregates the chain and Mongo lack: per-node rewards "
                "for every interval including the current one (rewards/node?address=), "
                "daily history (days=), node and validator performance, rETH DEX "
                "trades, the ETH price, oDAO activity, client diversity and more. "
                "Common params: address, days, limit, q, metric. Responses can be "
                "large; prefer summary endpoints and small limits. Endpoints:\n"
                + ROCKETDASH_ENDPOINTS,
                RocketDashInput,
                self.rocketdash_get,
            ),
            AgentTool(
                "defillama_get",
                "GET the DefiLlama API: TVL, yields and prices for any protocol, "
                "e.g. to compare Rocket Pool with Lido, ether.fi or other liquid "
                "staking. api: protocols, protocol/<slug>, tvl/<slug>, v2/chains, "
                "v2/historicalChainTvl/<chain>. yields: pools (all pools, large), "
                "chart/<pool id>. coins: prices/current/<chain:address,...>, "
                "prices/historical/<unix>/<coins>, chart/<coins>. Many responses "
                "are megabytes: filter lists with where, or call it from code.",
                DefiLlamaInput,
                self.defillama_get,
            ),
            AgentTool(
                "signaling_proposals",
                "pDAO signaling (off-chain snapshot) votes from rocketdash.net, newest "
                "first: choices with voting power and share, quorum and state. For "
                "ranked-choice votes the scores appear to be the final instant-runoff "
                "round, so eliminated choices show 0; signaling_votes has the full "
                "rankings.",
                SignalingProposalsInput,
                self.signaling_proposals,
            ),
            AgentTool(
                "signaling_votes",
                "The individual votes on one signaling proposal: node, voting power, "
                "choice, reason and whether it overrides a delegate. Also returns the "
                "forum discussion link.",
                SignalingVotesInput,
                self.signaling_votes,
            ),
        ]
        if self.github_token:
            tools.append(
                AgentTool(
                    "github_search",
                    f"Search code on GitHub, by default in the {GITHUB_ORG} "
                    "organization (contracts, smartnode, RPIPs, docs). Returns "
                    "matching files with snippets and raw URLs to read in full with "
                    "web_fetch.",
                    GithubSearchInput,
                    self.github_search,
                )
            )
        return tools

    async def block_time(self, args: BlockTimeInput) -> str:
        if (args.timestamp is None) == (args.block is None):
            raise ToolError("Pass exactly one of timestamp or block")
        if args.block is not None:
            ts = await block_to_ts(args.block)
            return _dump(
                {
                    "block": args.block,
                    "timestamp": ts,
                    "utc": datetime.fromtimestamp(ts, UTC).isoformat(),
                }
            )
        assert args.timestamp is not None
        target = int(_to_datetime(args.timestamp).timestamp())
        if target > datetime.now(UTC).timestamp():
            raise ToolError("That time is in the future")
        block = await ts_to_block(target)
        ts = await block_to_ts(block)
        return _dump(
            {
                "block": block,
                "timestamp": ts,
                "utc": datetime.fromtimestamp(ts, UTC).isoformat(),
            }
        )

    async def address_info(self, args: AddressInfoInput) -> str:
        if w3.is_address(args.address):
            address = w3.to_checksum_address(args.address)
            ens = await get_name(address)
        elif (resolved := await resolve_name(args.address)) is not None:
            address, ens = resolved, args.address
        else:
            raise ToolError(f"{args.address} is not an address or a known ENS name")
        is_node, is_minipool, is_megapool = await asyncio.gather(
            rp.is_node(address), rp.is_minipool(address), rp.is_megapool(address)
        )
        return _dump(
            {
                "address": address,
                "ens": ens,
                "label": await get_address_name(address),
                "rocket_pool_contract": rp.get_name_by_address(address),
                "node": is_node,
                "minipool": is_minipool,
                "megapool": is_megapool,
            }
        )

    async def rocketdash_get(self, args: RocketDashInput) -> str:
        endpoint = args.endpoint.strip("/").removeprefix("api/")
        if not _ROCKETDASH_PATH.match(endpoint) or endpoint.startswith(
            _ROCKETDASH_BLOCKED
        ):
            raise ToolError(f"{args.endpoint!r} is not an allowed endpoint")
        params = {"network": cfg.rocketpool.chain, **args.params}
        async with (
            aiohttp.ClientSession(timeout=ROCKETDASH_TIMEOUT) as session,
            session.get(ROCKETDASH_API + endpoint, params=params) as resp,
        ):
            if resp.status != 200:
                body = re.sub(r"<[^>]+>", " ", await resp.text())
                detail = " ".join(body.split())[:300]
                raise ToolError(f"RocketDash returned {resp.status}: {detail}")
            data = await resp.json(content_type=None)
        return _dump(data)

    async def defillama_get(self, args: DefiLlamaInput) -> str:
        path = args.path.strip("/")
        if not _DEFILLAMA_PATH.match(path) or ".." in path or "//" in path:
            raise ToolError(f"{args.path!r} is not a valid path")
        async with (
            aiohttp.ClientSession(timeout=DEFILLAMA_TIMEOUT) as session,
            session.get(DEFILLAMA_HOSTS[args.host] + path, params=args.params) as resp,
        ):
            if resp.status != 200:
                detail = " ".join((await resp.text()).split())[:300]
                raise ToolError(f"DefiLlama returned {resp.status}: {detail}")
            data = await resp.json(content_type=None)
        return _dump(_where(data, args.where))

    async def signaling_proposals(self, args: SignalingProposalsInput) -> str:
        proposals = await Signaling.fetch_proposals()
        if args.title:
            needle = args.title.lower()
            proposals = [p for p in proposals if needle in p.title.lower()]
        return _dump(
            {
                "total": len(proposals),
                "proposals": [_proposal_summary(p) for p in proposals[: args.limit]],
            }
        )

    async def signaling_votes(self, args: SignalingVotesInput) -> str:
        proposals = {p.id: p for p in await Signaling.fetch_proposals()}
        if (proposal := proposals.get(args.proposal_id)) is None:
            raise ToolError(
                f"No proposal {args.proposal_id}; find ids with signaling_proposals"
            )
        votes = sorted(
            await Signaling.fetch_votes(proposal), key=lambda v: v.power, reverse=True
        )
        return _dump(
            {
                "proposal": _proposal_summary(proposal),
                "discussion_url": await Signaling.fetch_discussion_url(proposal.id),
                "voters": len(votes),
                "votes": [
                    {
                        "node": v.node,
                        "signer": v.signer,
                        "time": v.timestamp,
                        "power": v.power,
                        "choice": _choice_labels(proposal.choices, v.choice),
                        "reason": v.reason,
                        "override": v.is_override,
                    }
                    for v in votes[: args.limit]
                ],
            }
        )

    async def github_search(self, args: GithubSearchInput) -> str:
        headers = {
            "Accept": "application/vnd.github.text-match+json",
            "Authorization": f"Bearer {self.github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        query = f"{args.query} org:{args.org}" if args.org else args.query
        params = {"q": query, "per_page": str(args.limit)}
        async with (
            aiohttp.ClientSession(timeout=GITHUB_TIMEOUT) as session,
            session.get(GITHUB_SEARCH_URL, params=params, headers=headers) as resp,
        ):
            data = await resp.json()
            if resp.status != 200:
                raise ToolError(
                    f"GitHub search failed ({resp.status}): {data.get('message')}"
                )
        return _dump(
            {
                "total": data["total_count"],
                "results": [
                    {
                        "repo": item["repository"]["full_name"],
                        "path": item["path"],
                        "url": _raw_url(item["html_url"]),
                        "matches": [
                            m["fragment"] for m in item.get("text_matches", [])
                        ],
                    }
                    for item in data["items"]
                ],
            }
        )

    async def _collection(self, name: str) -> AsyncCollection[dict[str, Any]]:
        if name not in await self.db.list_collection_names():
            raise ToolError(f"Unknown collection {name!r}")
        return self.db[name]

    async def mongo_list_collections(self, _: NoInput) -> str:
        names = sorted(await self.db.list_collection_names())
        return _dump(
            {name: await self.db[name].estimated_document_count() for name in names}
        )

    async def mongo_describe_collection(self, args: CollectionInput) -> str:
        collection = await self._collection(args.collection)
        cursor = await collection.aggregate([{"$sample": {"size": SAMPLE_SIZE}}])
        fields: dict[str, dict[str, Any]] = {}
        sampled = 0
        async for doc in cursor:
            sampled += 1
            _collect_fields(doc, "", fields)
        indexes = await collection.index_information()
        return _dump(
            {
                "documents": await collection.estimated_document_count(),
                "sampled": sampled,
                "indexes": [info["key"] for info in indexes.values()],
                "fields": {
                    path: {"types": sorted(f["types"]), "example": f["example"]}
                    for path, f in sorted(fields.items())
                },
            }
        )

    async def mongo_find(self, args: MongoFindInput) -> str:
        collection = await self._collection(args.collection)
        cursor = collection.find(
            _from_ejson(args.filter),
            args.projection,
            sort=list(args.sort.items()) if args.sort else None,
            skip=args.skip,
            limit=args.limit,
            max_time_ms=MAX_TIME_MS,
        )
        return _dump_bson(await cursor.to_list())

    async def mongo_aggregate(self, args: MongoAggregateInput) -> str:
        _check_read_only(args.pipeline)
        collection = await self._collection(args.collection)
        pipeline = [*_from_ejson(args.pipeline), {"$limit": MAX_DOCS + 1}]
        cursor = await collection.aggregate(pipeline, maxTimeMS=MAX_TIME_MS)
        docs = await cursor.to_list()
        if len(docs) > MAX_DOCS:
            return _dump_bson(
                {"documents": docs[:MAX_DOCS], "note": f"cut at {MAX_DOCS} documents"}
            )
        return _dump_bson(docs)

    async def list_contracts(self, _: NoInput) -> str:
        abi_names = sorted(
            p.name.removesuffix(".abi.json")
            for p in Path("contracts").glob("*.abi.json")
        )
        return _dump({"registered": dict(rp.addresses), "abi_only": abi_names})

    async def _contract(self, args: ContractInput) -> Any:
        if args.address:
            address = w3.to_checksum_address(args.address)
        else:
            try:
                address = await rp.get_address_by_name(args.contract)
            except NoAddressFound:
                raise ToolError(
                    f"No registered address for {args.contract}; pass an address"
                ) from None
        try:
            return await rp.assemble_contract(args.contract, address)
        except Exception as err:
            raise ToolError(f"No ABI for {args.contract}: {err}") from None

    async def contract_info(self, args: ContractInput) -> str:
        contract = await self._contract(args)
        entries = [dict(e) for e in contract.abi]
        return _dump(
            {
                "address": contract.address,
                "functions": [
                    _signature(e) for e in entries if e.get("type") == "function"
                ],
                "events": [e["name"] for e in entries if e.get("type") == "event"],
            }
        )

    async def _prepare_call(
        self, call: ContractCallSpec
    ) -> tuple[Any, list[dict[str, str]]]:
        """The bound contract function and its output names and types."""
        contract = await self._contract(call)
        candidates = [
            dict(e)
            for e in contract.abi
            if e.get("type") == "function"
            and e.get("name") == call.function
            and len(e.get("inputs", [])) == len(call.args)
        ]
        if not candidates:
            raise ToolError(
                f"{call.contract} has no function {call.function} taking "
                f"{len(call.args)} arguments; check contract_info"
            )
        entry = candidates[0]
        call_args = [
            _coerce_arg(RocketPool._abi_type_str(dict(i)), a)
            for i, a in zip(entry["inputs"], call.args, strict=True)
        ]
        outputs = [
            {"name": o.get("name", ""), "type": RocketPool._abi_type_str(dict(o))}
            for o in entry.get("outputs", [])
        ]
        return contract.functions[call.function](*call_args), outputs

    async def contract_call(self, args: ContractCallInput) -> str:
        fn, outputs = await self._prepare_call(args)
        try:
            result = await fn.call(block_identifier=_block_id(args.block))
        except ContractLogicError as err:
            raise ToolError(f"Call reverted: {err}") from None
        return _dump({"result": result, "outputs": outputs})

    async def contract_call_many(self, args: ContractCallManyInput) -> str:
        fns = []
        outputs: dict[str, list[dict[str, str]]] = {}
        for i, call in enumerate(args.calls):
            try:
                (
                    fn,
                    outputs[f"{call.contract}.{call.function}"],
                ) = await self._prepare_call(call)
            except ToolError as err:
                raise ToolError(f"Call {i}: {err}") from None
            fns.append(fn)
        results = await rp.multicall(
            fns, require_success=False, block=_block_id(args.block)
        )
        return _dump({"results": results, "outputs": outputs})

    async def contract_events(self, args: ContractEventsInput) -> str:
        contract = await self._contract(args)
        entry = next(
            (
                dict(e)
                for e in contract.abi
                if e.get("type") == "event" and e.get("name") == args.event
            ),
            None,
        )
        if entry is None:
            raise ToolError(f"{args.contract} has no event {args.event}")
        latest = int(await w3.eth.get_block_number())
        from_block = _resolve_block(args.from_block, latest)
        to_block = _resolve_block(args.to_block, latest)
        if to_block - from_block > MAX_EVENT_BLOCK_RANGE:
            raise ToolError(
                f"Range exceeds {MAX_EVENT_BLOCK_RANGE} blocks; split it up"
            )

        indexed = {i["name"]: i["type"] for i in entry["inputs"] if i.get("indexed")}
        if unknown := set(args.filters) - set(indexed):
            raise ToolError(f"Not indexed arguments: {sorted(unknown)}")
        filters = {
            name: [_coerce_arg(indexed[name], v) for v in value]
            if isinstance(value, list)
            else _coerce_arg(indexed[name], value)
            for name, value in args.filters.items()
        }
        logs = await get_logs(
            contract.events[args.event],
            BlockNumber(from_block),
            BlockNumber(to_block),
            filters or None,
        )
        newest = sorted(
            logs, key=lambda e: (e["blockNumber"], e["logIndex"]), reverse=True
        )
        return _dump(
            {
                "total": len(logs),
                "events": [
                    {
                        "block": e["blockNumber"],
                        "tx": e["transactionHash"],
                        "address": e["address"],
                        "args": e["args"],
                    }
                    for e in newest[: args.limit]
                ],
            }
        )

    @staticmethod
    async def _check_log_range(params: list[Any]) -> None:
        if not params or not isinstance(params[0], dict) or "blockHash" in params[0]:
            return
        latest = int(await w3.eth.get_block_number())
        span = _resolve_block(params[0].get("toBlock"), latest) - _resolve_block(
            params[0].get("fromBlock"), latest
        )
        if span > MAX_LOG_BLOCK_RANGE:
            raise ToolError(
                f"Range of {span} blocks exceeds {MAX_LOG_BLOCK_RANGE}; split it up"
            )

    async def eth_rpc(self, args: EthRpcInput) -> str:
        if not args.method.startswith(_RPC_PREFIXES) or args.method.startswith(
            _RPC_BLOCKED
        ):
            raise ToolError(f"{args.method} is not an allowed read-only method")
        if args.method == "eth_getLogs":
            await self._check_log_range(args.params)
        response = await w3.provider.make_request(RPCEndpoint(args.method), args.params)
        if error := response.get("error"):
            raise ToolError(f"RPC error: {_dump(error)}")
        return _dump(response.get("result"))

    async def beacon_get(self, args: BeaconGetInput) -> str:
        if not args.path.startswith("/eth/"):
            raise ToolError("Path must start with /eth/")
        if _UNFILTERED_VALIDATORS.match(args.path) and "id" not in args.params:
            raise ToolError("Pass an id filter; the full validator set is too large")
        return _dump(
            await bacon._async_make_get_request(args.path, params=args.params or None)
        )

    async def node_collateral(self, args: NodeCollateralInput) -> str:
        nodes = await get_node_collateral_data(self.db)
        rpl_price = solidity.to_float(await rp.call("rocketNetworkPrices.getRPLPrice"))

        def with_ratios(node: Mapping[str, Any]) -> dict[str, Any]:
            value = node["rpl_stake"] * rpl_price
            return {
                **node,
                "rpl_value_eth": value,
                "pct_of_bonded": value / node["bonded"] * 100
                if node["bonded"]
                else None,
                "pct_of_borrowed": (
                    value / node["borrowed"] * 100 if node["borrowed"] else None
                ),
            }

        if args.node_address:
            address = w3.to_checksum_address(args.node_address)
            if address not in nodes:
                raise ToolError(
                    "Not an active node (no staking minipools or active megapool validators)"
                )
            return _dump({"rpl_price_eth": rpl_price, **with_ratios(nodes[address])})

        rows = [with_ratios(n) for n in nodes.values()]
        percentiles = [10, 25, 50, 75, 90]

        def spread(key: str) -> dict[str, float]:
            values = [r[key] for r in rows if r[key] is not None]
            if not values:
                return {}
            return {
                f"p{p}": float(v)
                for p, v in zip(
                    percentiles, np.percentile(values, percentiles), strict=True
                )
            }

        return _dump(
            {
                "rpl_price_eth": rpl_price,
                "active_nodes": len(rows),
                "nodes_without_rpl": sum(1 for r in rows if not r["rpl_stake"]),
                "total_bonded_eth": sum(r["bonded"] for r in rows),
                "total_borrowed_eth": sum(r["borrowed"] for r in rows),
                "total_rpl_stake": sum(r["rpl_stake"] for r in rows),
                "pct_of_bonded": spread("pct_of_bonded"),
                "pct_of_borrowed": spread("pct_of_borrowed"),
            }
        )

    def _file_name(self, base: str, extension: str) -> str:
        if len(self.files) >= MAX_FILES:
            raise ToolError(f"At most {MAX_FILES} files per answer")
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", base).strip("_")[:50] or "file"
        name = f"{stem}.{extension}"
        counter = 2
        while name in self.files:
            name = f"{stem}_{counter}.{extension}"
            counter += 1
        return name

    async def save_file(self, args: FileInput) -> str:
        stem, _, extension = args.filename.rpartition(".")
        if extension.lower() not in _FILE_EXTENSIONS:
            raise ToolError(f"Extension must be one of {sorted(_FILE_EXTENSIONS)}")
        name = self._file_name(stem, extension.lower())
        self.files[name] = args.content.encode()
        return f"Saved {name}"

    async def make_chart(self, args: ChartInput) -> str:
        for s in args.series:
            if len(s.x) != len(s.y):
                raise ToolError(f"Series {s.label!r} has {len(s.x)} x and {len(s.y)} y")
        if (
            args.kind == "area"
            and args.stacked
            and len({len(s.x) for s in args.series}) > 1
        ):
            raise ToolError("Stacked area series must share the same x values")

        categorical = args.kind == "bar" or args.x_type == "category"
        categories: list[str] = []
        if categorical:
            for s in args.series:
                for x in s.x:
                    label = self._category_label(x, args.x_type)
                    if label not in categories:
                        categories.append(label)

        def xs_for(s: ChartSeries) -> list[Any]:
            if categorical:
                return [
                    categories.index(self._category_label(x, args.x_type)) for x in s.x
                ]
            if args.x_type == "time":
                return [_to_datetime(x) for x in s.x]
            return [_to_number(x) for x in s.x]

        series_xs = [xs_for(s) for s in args.series]
        name = self._file_name(args.filename, "png")

        def draw(fig: Figure) -> None:
            fig.set_facecolor(SURFACE)
            ax = fig.add_subplot()
            ax.set_facecolor(SURFACE)
            match args.kind:
                case "bar":
                    self._draw_bars(ax, args, series_xs, len(categories))
                case "area" if args.stacked:
                    ax.stackplot(
                        series_xs[0],
                        [s.y for s in args.series],
                        labels=[s.label for s in args.series],
                        colors=PALETTE,
                        edgecolor=SURFACE,
                        linewidth=1,
                    )
                case _:
                    for i, (s, xs) in enumerate(
                        zip(args.series, series_xs, strict=True)
                    ):
                        color = PALETTE[i]
                        if args.kind == "scatter":
                            ax.scatter(
                                xs,
                                s.y,
                                s=40,
                                color=color,
                                label=s.label,
                                edgecolors=SURFACE,
                                linewidths=1,
                            )
                            continue
                        ax.plot(xs, s.y, color=color, linewidth=2, label=s.label)
                        if args.kind == "area":
                            ax.fill_between(
                                xs, s.y, color=color, alpha=0.2, linewidth=0
                            )
            self._style_axes(fig, ax, args, categories)

        self.files[name] = (
            await render_png(draw, figsize=(10, 5.5), dpi=150, bbox_inches="tight")
        ).getvalue()
        return f"Saved {name}"

    @staticmethod
    def _category_label(x: float | str, x_type: str) -> str:
        if x_type == "time":
            return _to_datetime(x).strftime("%Y-%m-%d")
        if isinstance(x, float) and x.is_integer():
            return str(int(x))
        return str(x)

    @staticmethod
    def _draw_bars(
        ax: Any, args: ChartInput, series_xs: list[list[Any]], n_categories: int
    ) -> None:
        bottoms = np.zeros(n_categories)
        group = len(args.series)
        width = 0.8 if args.stacked else 0.8 / group
        for i, (s, xs) in enumerate(zip(args.series, series_xs, strict=True)):
            positions = np.array(xs, dtype=float)
            heights = np.array(s.y, dtype=float)
            if args.stacked:
                ax.bar(
                    positions,
                    heights,
                    width,
                    bottom=bottoms[positions.astype(int)],
                    color=PALETTE[i],
                    label=s.label,
                    edgecolor=SURFACE,
                    linewidth=1,
                )
                np.add.at(bottoms, positions.astype(int), heights)
            else:
                offset = (i - (group - 1) / 2) * width
                ax.bar(
                    positions + offset,
                    heights,
                    width,
                    color=PALETTE[i],
                    label=s.label,
                    edgecolor=SURFACE,
                    linewidth=1,
                )

    @staticmethod
    def _style_axes(
        fig: Figure, ax: Any, args: ChartInput, categories: list[str]
    ) -> None:
        ax.set_title(args.title, color=INK, loc="left", fontsize=13, pad=12)
        ax.set_xlabel(args.x_label, color=MUTED)
        ax.set_ylabel(args.y_label, color=MUTED)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(AXIS)
        ax.tick_params(colors=MUTED)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        if args.log_y:
            ax.set_yscale("log")
        if categories:
            ax.set_xticks(range(len(categories)))
            many = len(categories) > 8
            ax.set_xticklabels(
                categories, rotation=45 if many else 0, ha="right" if many else "center"
            )
        elif args.x_type == "time":
            fig.autofmt_xdate()
        if len(args.series) > 1:
            ax.legend(frameon=False, labelcolor=INK)
