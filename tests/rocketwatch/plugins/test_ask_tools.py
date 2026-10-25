import json
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest
from pymongo.asynchronous.database import AsyncDatabase
from web3.exceptions import ContractLogicError

from rocketwatch.plugins.ask import tools as tools_module
from rocketwatch.plugins.ask.tools import (
    MAX_DOCS,
    MAX_FILES,
    MAX_LOG_BLOCK_RANGE,
    MAX_RESULT_CHARS,
    MAX_SCRIPT_RESULT_CHARS,
    AddressInfoInput,
    AskTools,
    BeaconGetInput,
    BlockTimeInput,
    CollectionInput,
    ContractCallInput,
    ContractCallManyInput,
    ContractEventsInput,
    ContractInput,
    DefiLlamaInput,
    DiscordMessagesInput,
    EthRpcInput,
    FileInput,
    GithubSearchInput,
    MongoAggregateInput,
    MongoFindInput,
    NodeCollateralInput,
    NoInput,
    RocketDashInput,
    SignalingProposalsInput,
    SignalingVotesInput,
)
from rocketwatch.plugins.signaling.signaling import Proposal, Signaling, Vote
from rocketwatch.utils import agent, rocketpool, shared_w3
from rocketwatch.utils.agent import ToolError

NODE = "0x" + "11" * 20


@pytest.fixture
def tools() -> AskTools:
    return AskTools(MagicMock())


@pytest.fixture
def node_rpc(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    w3_stub = MagicMock()
    w3_stub.provider.make_request = AsyncMock(return_value={"result": "0x10"})
    w3_stub.eth.get_block_number = AsyncMock(return_value=20_000_000)
    w3_stub.to_checksum_address = lambda a: a
    monkeypatch.setattr(shared_w3.w3, "_instance", w3_stub)
    return w3_stub.provider.make_request


class TestToolDefinitions:
    def test_every_tool_has_a_schema(self, tools: AskTools) -> None:
        for tool in tools.definitions():
            assert tool.input_model.model_json_schema()["type"] == "object"

    def test_names_are_unique(self, tools: AskTools) -> None:
        names = [tool.name for tool in tools.definitions()]
        assert len(names) == len(set(names))


class TestEthRpc:
    async def test_returns_result(self, tools: AskTools, node_rpc: AsyncMock) -> None:
        result = await tools.eth_rpc(
            EthRpcInput(method="eth_getBalance", params=[NODE, "latest"])
        )
        assert json.loads(result) == "0x10"
        node_rpc.assert_awaited_once_with("eth_getBalance", [NODE, "latest"])

    @pytest.mark.parametrize(
        "method",
        ["eth_sendRawTransaction", "eth_sign", "admin_addPeer", "personal_unlock"],
    )
    async def test_rejects_non_read_methods(
        self, tools: AskTools, node_rpc: AsyncMock, method: str
    ) -> None:
        with pytest.raises(ToolError):
            await tools.eth_rpc(EthRpcInput(method=method))
        node_rpc.assert_not_awaited()

    async def test_rpc_error_is_reported(
        self, tools: AskTools, node_rpc: AsyncMock
    ) -> None:
        node_rpc.return_value = {"error": {"code": -32000, "message": "bad block"}}
        with pytest.raises(ToolError, match="bad block"):
            await tools.eth_rpc(EthRpcInput(method="eth_getBlockByNumber"))

    @pytest.mark.parametrize(
        "log_filter",
        [
            {"fromBlock": "earliest"},
            {
                "fromBlock": hex(20_000_000 - MAX_LOG_BLOCK_RANGE - 1),
                "toBlock": "latest",
            },
            {"fromBlock": 0, "toBlock": MAX_LOG_BLOCK_RANGE + 1},
        ],
    )
    async def test_rejects_wide_log_ranges(
        self, tools: AskTools, node_rpc: AsyncMock, log_filter: dict[str, Any]
    ) -> None:
        with pytest.raises(ToolError, match="split"):
            await tools.eth_rpc(EthRpcInput(method="eth_getLogs", params=[log_filter]))
        node_rpc.assert_not_awaited()

    @pytest.mark.parametrize(
        "log_filter",
        [
            {"fromBlock": hex(20_000_000 - 1000)},
            {"fromBlock": 100, "toBlock": 200},
            {"blockHash": "0x" + "ab" * 32},
        ],
    )
    async def test_allows_narrow_log_queries(
        self, tools: AskTools, node_rpc: AsyncMock, log_filter: dict[str, Any]
    ) -> None:
        await tools.eth_rpc(EthRpcInput(method="eth_getLogs", params=[log_filter]))
        node_rpc.assert_awaited_once()


class TestBeaconGet:
    @pytest.fixture
    def beacon(self, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        stub = MagicMock()
        stub._async_make_get_request = AsyncMock(return_value={"data": {"slot": "1"}})
        monkeypatch.setattr(shared_w3.bacon, "_instance", stub)
        return stub._async_make_get_request

    async def test_returns_response(self, tools: AskTools, beacon: AsyncMock) -> None:
        result = await tools.beacon_get(
            BeaconGetInput(path="/eth/v1/beacon/headers/head")
        )
        assert json.loads(result) == {"data": {"slot": "1"}}

    async def test_validator_lookup_with_id(
        self, tools: AskTools, beacon: AsyncMock
    ) -> None:
        args = BeaconGetInput(
            path="/eth/v1/beacon/states/head/validators", params={"id": "1,2"}
        )
        await tools.beacon_get(args)
        beacon.assert_awaited_once_with(
            "/eth/v1/beacon/states/head/validators", params={"id": "1,2"}
        )

    @pytest.mark.parametrize(
        "path",
        [
            "/eth/v1/beacon/states/head/validators",
            "/eth/v1/beacon/states/finalized/validator_balances",
            "/admin/peers",
        ],
    )
    async def test_rejects_unbounded_or_foreign_paths(
        self, tools: AskTools, beacon: AsyncMock, path: str
    ) -> None:
        with pytest.raises(ToolError):
            await tools.beacon_get(BeaconGetInput(path=path))
        beacon.assert_not_awaited()


class TestContracts:
    ABI: ClassVar[list[dict[str, Any]]] = [
        {
            "type": "function",
            "name": "getNodeFee",
            "stateMutability": "view",
            "inputs": [
                {"name": "node", "type": "address"},
                {"name": "amount", "type": "uint256"},
            ],
            "outputs": [{"name": "fee", "type": "uint256"}],
        },
        {
            "type": "event",
            "name": "NodeRegistered",
            "inputs": [
                {"name": "node", "type": "address", "indexed": True},
                {"name": "time", "type": "uint256", "indexed": False},
            ],
        },
    ]

    @pytest.fixture
    def function(self, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        function = MagicMock()
        function.return_value.call = AsyncMock(return_value=10**18)
        contract = MagicMock()
        contract.abi = self.ABI
        contract.address = NODE
        contract.functions = {"getNodeFee": function}
        contract.events = {"NodeRegistered": MagicMock(name="NodeRegistered")}
        rp_stub = MagicMock()
        rp_stub.get_address_by_name = AsyncMock(return_value=NODE)
        rp_stub.assemble_contract = AsyncMock(return_value=contract)
        rp_stub.addresses = {"rocketNodeManager": NODE}
        monkeypatch.setattr(rocketpool.rp, "_instance", rp_stub)
        monkeypatch.setattr(shared_w3.w3, "_instance", MagicMock())
        shared_w3.w3.to_checksum_address = lambda a: a.lower()
        return function

    async def test_call_coerces_args_and_returns_typed_result(
        self, tools: AskTools, function: MagicMock
    ) -> None:
        result = await tools.contract_call(
            ContractCallInput(
                contract="rocketNodeManager",
                function="getNodeFee",
                args=[NODE.upper().replace("0X", "0x"), "1000000000000000000"],
                block="123",
            )
        )
        function.assert_called_once_with(NODE.lower(), 10**18)
        function.return_value.call.assert_awaited_once_with(block_identifier=123)
        assert json.loads(result) == {
            "result": 10**18,
            "outputs": [{"name": "fee", "type": "uint256"}],
        }

    async def test_unknown_function(self, tools: AskTools, function: MagicMock) -> None:
        with pytest.raises(ToolError, match="contract_info"):
            await tools.contract_call(
                ContractCallInput(contract="rocketNodeManager", function="nope")
            )

    async def test_wrong_arity(self, tools: AskTools, function: MagicMock) -> None:
        with pytest.raises(ToolError):
            await tools.contract_call(
                ContractCallInput(
                    contract="rocketNodeManager", function="getNodeFee", args=[NODE]
                )
            )

    async def test_revert_is_reported(
        self, tools: AskTools, function: MagicMock
    ) -> None:
        function.return_value.call.side_effect = ContractLogicError("Invalid node")
        with pytest.raises(ToolError, match="reverted"):
            await tools.contract_call(
                ContractCallInput(
                    contract="rocketNodeManager",
                    function="getNodeFee",
                    args=[NODE, 1],
                )
            )

    async def test_call_many_is_one_multicall(
        self, tools: AskTools, function: MagicMock
    ) -> None:
        rocketpool.rp.multicall = AsyncMock(return_value=[5, None])
        call = {"contract": "rocketNodeManager", "function": "getNodeFee"}
        result = await tools.contract_call_many(
            ContractCallManyInput.model_validate(
                {
                    "calls": [{**call, "args": [NODE, 1]}, {**call, "args": [NODE, 2]}],
                    "block": 123,
                }
            )
        )
        rocketpool.rp.multicall.assert_awaited_once()
        fns = rocketpool.rp.multicall.await_args.args[0]
        assert len(fns) == 2
        assert rocketpool.rp.multicall.await_args.kwargs["block"] == 123
        assert [c.args for c in function.call_args_list] == [
            (NODE.lower(), 1),
            (NODE.lower(), 2),
        ]
        assert json.loads(result) == {
            "results": [5, None],
            "outputs": {
                "rocketNodeManager.getNodeFee": [{"name": "fee", "type": "uint256"}]
            },
        }

    async def test_call_many_names_the_bad_call(
        self, tools: AskTools, function: MagicMock
    ) -> None:
        rocketpool.rp.multicall = AsyncMock()
        calls = [
            {
                "contract": "rocketNodeManager",
                "function": "getNodeFee",
                "args": [NODE, 1],
            },
            {"contract": "rocketNodeManager", "function": "nope"},
        ]
        with pytest.raises(ToolError, match="Call 1"):
            await tools.contract_call_many(
                ContractCallManyInput.model_validate({"calls": calls})
            )
        rocketpool.rp.multicall.assert_not_awaited()

    async def test_unregistered_contract(
        self, tools: AskTools, function: MagicMock
    ) -> None:
        rocketpool.rp.get_address_by_name.side_effect = rocketpool.NoAddressFound()
        with pytest.raises(ToolError, match="pass an address"):
            await tools.contract_info(ContractInput(contract="ERC20"))

    async def test_info_lists_signatures(
        self, tools: AskTools, function: MagicMock
    ) -> None:
        info = json.loads(
            await tools.contract_info(ContractInput(contract="rocketNodeManager"))
        )
        assert info["address"] == NODE
        assert info["functions"] == [
            "getNodeFee(address node, uint256 amount) view returns (uint256 fee)"
        ]
        assert info["events"] == ["NodeRegistered"]

    @pytest.fixture
    def logs(self, monkeypatch: pytest.MonkeyPatch, function: MagicMock) -> AsyncMock:
        shared_w3.w3.eth.get_block_number = AsyncMock(return_value=2_000_000)
        logs = AsyncMock(
            return_value=[
                {
                    "blockNumber": block,
                    "logIndex": 0,
                    "transactionHash": bytes([block % 256]) * 32,
                    "address": NODE,
                    "args": {"node": NODE, "time": block},
                }
                for block in (1_500_000, 1_900_000, 1_700_000)
            ]
        )
        monkeypatch.setattr(tools_module, "get_logs", logs)
        return logs

    async def test_events_newest_first_with_limit(
        self, tools: AskTools, logs: AsyncMock
    ) -> None:
        result = json.loads(
            await tools.contract_events(
                ContractEventsInput(
                    contract="rocketNodeManager",
                    event="NodeRegistered",
                    from_block=1_400_000,
                    limit=2,
                )
            )
        )
        assert result["total"] == 3
        assert [e["block"] for e in result["events"]] == [1_900_000, 1_700_000]
        assert result["events"][0]["args"] == {"node": NODE, "time": 1_900_000}
        assert result["events"][0]["tx"].startswith("0x")
        _, from_block, to_block, filters = logs.call_args.args
        assert (from_block, to_block, filters) == (1_400_000, 2_000_000, None)

    async def test_events_filter_on_indexed_args(
        self, tools: AskTools, logs: AsyncMock
    ) -> None:
        await tools.contract_events(
            ContractEventsInput(
                contract="rocketNodeManager",
                event="NodeRegistered",
                from_block="latest",
                filters={"node": [NODE.upper().replace("0X", "0x")]},
            )
        )
        assert logs.call_args.args[3] == {"node": [NODE.lower()]}

    @pytest.mark.parametrize(
        ("event", "from_block", "filters", "match"),
        [
            ("Missing", 1_900_000, {}, "no event"),
            ("NodeRegistered", "earliest", {}, "split"),
            ("NodeRegistered", 1_900_000, {"time": 1}, "Not indexed"),
        ],
    )
    async def test_events_rejects_bad_queries(
        self,
        tools: AskTools,
        logs: AsyncMock,
        event: str,
        from_block: int | str,
        filters: dict[str, Any],
        match: str,
    ) -> None:
        with pytest.raises(ToolError, match=match):
            await tools.contract_events(
                ContractEventsInput(
                    contract="rocketNodeManager",
                    event=event,
                    from_block=from_block,
                    filters=filters,
                )
            )
        logs.assert_not_awaited()

    async def test_list_contracts(self, tools: AskTools, function: MagicMock) -> None:
        listed = json.loads(await tools.list_contracts(NoInput()))
        assert listed["registered"] == {"rocketNodeManager": NODE}


class TestNodeCollateral:
    @pytest.fixture(autouse=True)
    def data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        nodes = {
            NODE: {
                "bonded": 8.0,
                "borrowed": 24.0,
                "rpl_stake": 100.0,
                "validators": 1,
            },
            "0x" + "22" * 20: {
                "bonded": 16.0,
                "borrowed": 16.0,
                "rpl_stake": 0.0,
                "validators": 1,
            },
        }
        monkeypatch.setattr(
            tools_module, "get_node_collateral_data", AsyncMock(return_value=nodes)
        )
        rp_stub = MagicMock()
        rp_stub.call = AsyncMock(return_value=10**16)  # 0.01 ETH per RPL
        monkeypatch.setattr(rocketpool.rp, "_instance", rp_stub)
        w3_stub = MagicMock()
        w3_stub.to_checksum_address = lambda a: a
        monkeypatch.setattr(shared_w3.w3, "_instance", w3_stub)

    async def test_single_node(self, tools: AskTools) -> None:
        node = json.loads(
            await tools.node_collateral(NodeCollateralInput(node_address=NODE))
        )
        assert node["rpl_value_eth"] == pytest.approx(1.0)
        assert node["pct_of_bonded"] == pytest.approx(12.5)
        assert node["pct_of_borrowed"] == pytest.approx(100 / 24)

    async def test_inactive_node(self, tools: AskTools) -> None:
        with pytest.raises(ToolError):
            await tools.node_collateral(
                NodeCollateralInput(node_address="0x" + "33" * 20)
            )

    async def test_network_summary(self, tools: AskTools) -> None:
        summary = json.loads(await tools.node_collateral(NodeCollateralInput()))
        assert summary["active_nodes"] == 2
        assert summary["nodes_without_rpl"] == 1
        assert summary["total_bonded_eth"] == pytest.approx(24.0)
        assert summary["pct_of_bonded"]["p50"] == pytest.approx(6.25)


class TestFiles:
    async def test_saves_with_sanitized_unique_names(self, tools: AskTools) -> None:
        first = await tools.save_file(
            FileInput(filename="node list.csv", content="a,b")
        )
        second = await tools.save_file(FileInput(filename="node list.csv", content="c"))
        assert first == "Saved node_list.csv"
        assert second == "Saved node_list_2.csv"
        assert tools.files == {"node_list.csv": b"a,b", "node_list_2.csv": b"c"}

    async def test_rejects_other_extensions(self, tools: AskTools) -> None:
        with pytest.raises(ToolError):
            await tools.save_file(FileInput(filename="run.sh", content="rm -rf /"))

    async def test_file_limit(self, tools: AskTools) -> None:
        for i in range(MAX_FILES):
            await tools.save_file(FileInput(filename=f"f{i}.txt", content=""))
        with pytest.raises(ToolError):
            await tools.save_file(FileInput(filename="extra.txt", content=""))


def _proposal(pid: str, title: str, **overrides: Any) -> Proposal:
    values: dict[str, Any] = {
        "id": pid,
        "title": title,
        "vote_type": "single-choice",
        "choices": ["For", "Against", "Abstain"],
        "start": 1_700_000_000,
        "end": 1_700_600_000,
        "state": "closed",
        "lifecycle": "ready",
        "scores": [750.0, 200.0, 50.0],
        "scores_total": 1000.0,
        "quorum": 900.0,
    }
    values.update(overrides)
    return Proposal(**values)


class TestSignaling:
    NEWEST: ClassVar[Proposal] = _proposal("0xb", "RPIP-71 exit criterion")
    OLDER: ClassVar[Proposal] = _proposal(
        "0xa",
        "Ranked RPIP vote",
        vote_type="ranked-choice",
        choices=["A", "B", "C"],
        scores=[10.0, 0.0, 0.0],
        scores_total=10.0,
    )

    @pytest.fixture(autouse=True)
    def api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            Signaling,
            "fetch_proposals",
            AsyncMock(return_value=[self.NEWEST, self.OLDER]),
        )

        async def votes(proposal: Proposal) -> list[Vote]:
            return [
                Vote(proposal, NODE, NODE, 1_700_000_100, 5.0, [3, 1], "", False),
                Vote(
                    proposal, NODE, NODE, 1_700_000_200, 9.0, {"2": 3, "1": 1}, "", True
                ),
                Vote(proposal, NODE, NODE, 1_700_000_300, 1.0, 2, "meh", False),
            ]

        monkeypatch.setattr(Signaling, "fetch_votes", AsyncMock(side_effect=votes))
        monkeypatch.setattr(
            Signaling,
            "fetch_discussion_url",
            AsyncMock(return_value="https://dao.rocketpool.net/t/1"),
        )

    async def test_lists_results(self, tools: AskTools) -> None:
        listed = json.loads(
            await tools.signaling_proposals(SignalingProposalsInput(limit=1))
        )
        assert listed["total"] == 2
        [newest] = listed["proposals"]
        assert newest["title"] == "RPIP-71 exit criterion"
        assert newest["results"][0] == {"choice": "For", "power": 750.0, "pct": 75.0}
        assert newest["reached_quorum"] is True
        assert newest["url"].endswith("/vote/0xb")

    async def test_filters_by_title(self, tools: AskTools) -> None:
        listed = json.loads(
            await tools.signaling_proposals(SignalingProposalsInput(title="ranked"))
        )
        assert [p["id"] for p in listed["proposals"]] == ["0xa"]

    async def test_votes_with_readable_choices(self, tools: AskTools) -> None:
        result = json.loads(
            await tools.signaling_votes(SignalingVotesInput(proposal_id="0xa"))
        )
        assert result["discussion_url"] == "https://dao.rocketpool.net/t/1"
        assert result["voters"] == 3
        assert [v["power"] for v in result["votes"]] == [9.0, 5.0, 1.0]
        assert [v["choice"] for v in result["votes"]] == [
            {"B": 3, "A": 1},
            ["C", "A"],
            "B",
        ]

    async def test_unknown_proposal(self, tools: AskTools) -> None:
        with pytest.raises(ToolError, match="signaling_proposals"):
            await tools.signaling_votes(SignalingVotesInput(proposal_id="0xnope"))


class _HttpResponse:
    def __init__(self, status: int, data: Any) -> None:
        self.status = status
        self._data = data

    async def __aenter__(self) -> "_HttpResponse":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def json(self, **_: Any) -> Any:
        return self._data

    async def text(self) -> str:
        return str(self._data)


class _HttpSession:
    def __init__(self, response: _HttpResponse) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_HttpSession":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    def get(self, url: str, **kwargs: Any) -> _HttpResponse:
        self.requests.append({"url": url, **kwargs})
        return self.response


def _http(monkeypatch: pytest.MonkeyPatch, status: int, data: Any) -> _HttpSession:
    session = _HttpSession(_HttpResponse(status, data))
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: session)
    return session


def _discord_message(
    message_id: int, content: str, reply_to: int | None = None
) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.id = message_id
    message.jump_url = f"https://discord.com/channels/1/2/{message_id}"
    message.author.display_name = f"user{message_id}"
    message.author.bot = False
    message.created_at = datetime(2026, 10, 24, 21, message_id % 60, tzinfo=UTC)
    message.content = content
    message.clean_content = content
    message.poll = None
    message.reactions = []
    message.stickers = []
    message.message_snapshots = []
    message.embeds = []
    message.components = []
    message.attachments = []
    if reply_to is None:
        message.reference = None
    else:
        message.reference.message_id = reply_to
        message.reference.jump_url = f"https://discord.com/channels/1/2/{reply_to}"
    return message


class TestDiscordMessages:
    LINK = "https://discord.com/channels/1/2/20"

    @pytest.fixture
    def channel(self) -> MagicMock:
        history = {
            "before": [_discord_message(19, "second"), _discord_message(18, "first")],
            "after": [_discord_message(21, "reply", reply_to=20)],
        }

        def read(limit: int, **kwargs: Any) -> Any:
            async def gen() -> Any:
                for m in history["before" if "before" in kwargs else "after"][:limit]:
                    yield m

            return gen()

        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=_discord_message(20, "linked"))
        channel.history = MagicMock(side_effect=read)
        return channel

    @pytest.fixture
    def tools(self, channel: MagicMock) -> AskTools:
        client = MagicMock()
        client.get_channel = MagicMock(return_value=channel)
        return AskTools(MagicMock(), discord_client=client)

    def test_only_offered_with_a_client(self) -> None:
        assert "discord_messages" not in {
            t.name for t in AskTools(MagicMock()).definitions()
        }

    async def test_message_with_context_in_order(self, tools: AskTools) -> None:
        result = json.loads(
            (
                await tools.discord_messages(
                    DiscordMessagesInput(link=self.LINK, before=2, after=1)
                )
            ).text
        )
        assert result["linked"] == self.LINK
        assert [m["text"] for m in result["messages"]] == [
            "first",
            "second",
            "linked",
            "reply",
        ]
        # replies point back, so the model can follow a chain
        assert result["messages"][3]["reply_to"] == self.LINK

    async def test_just_the_message(self, tools: AskTools, channel: MagicMock) -> None:
        result = json.loads(
            (await tools.discord_messages(DiscordMessagesInput(link=self.LINK))).text
        )
        assert [m["text"] for m in result["messages"]] == ["linked"]
        channel.history.assert_not_called()

    async def test_unreadable_channel(
        self, tools: AskTools, channel: MagicMock
    ) -> None:
        channel.fetch_message.side_effect = discord.Forbidden(
            MagicMock(status=403), "no access"
        )
        with pytest.raises(ToolError, match="can't read"):
            await tools.discord_messages(DiscordMessagesInput(link=self.LINK))

    async def test_not_a_link(self, tools: AskTools) -> None:
        with pytest.raises(ToolError, match="Not a Discord message link"):
            await tools.discord_messages(
                DiscordMessagesInput(link="https://example.com/channels/1/2/3")
            )


class TestGithubSearch:
    HIT: ClassVar[dict[str, Any]] = {
        "path": "contracts/contract/megapool/RocketMegapoolDelegate.sol",
        "html_url": "https://github.com/rocket-pool/rocketpool/blob/abc123/contracts/contract/megapool/RocketMegapoolDelegate.sol",
        "repository": {"full_name": "rocket-pool/rocketpool"},
        "text_matches": [{"fragment": "function dequeue(uint32 _validatorId)"}],
    }

    def test_only_offered_with_token(self) -> None:
        names = {t.name for t in AskTools(MagicMock()).definitions()}
        assert "github_search" not in names
        names = {t.name for t in AskTools(MagicMock(), "token").definitions()}
        assert "github_search" in names

    async def test_searches_rocket_pool_org(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _http(monkeypatch, 200, {"total_count": 1, "items": [self.HIT]})
        result = json.loads(
            await AskTools(MagicMock(), "token").github_search(
                GithubSearchInput(query="dequeue", limit=5)
            )
        )

        [request] = session.requests
        assert request["params"]["q"] == "dequeue org:rocket-pool"
        assert request["params"]["per_page"] == "5"
        assert request["headers"]["Authorization"] == "Bearer token"
        assert result == {
            "total": 1,
            "results": [
                {
                    "repo": "rocket-pool/rocketpool",
                    "path": self.HIT["path"],
                    "url": "https://raw.githubusercontent.com/rocket-pool/rocketpool/abc123/contracts/contract/megapool/RocketMegapoolDelegate.sol",
                    "matches": ["function dequeue(uint32 _validatorId)"],
                }
            ],
        }

    @pytest.mark.parametrize(
        ("org", "query"),
        [
            ("ethereum", "SECONDS_PER_SLOT org:ethereum"),
            (None, "SECONDS_PER_SLOT"),
        ],
    )
    async def test_other_orgs(
        self, monkeypatch: pytest.MonkeyPatch, org: str | None, query: str
    ) -> None:
        session = _http(monkeypatch, 200, {"total_count": 0, "items": []})
        await AskTools(MagicMock(), "token").github_search(
            GithubSearchInput(query="SECONDS_PER_SLOT", org=org)
        )
        assert session.requests[0]["params"]["q"] == query

    @pytest.mark.parametrize(
        ("query", "sent"),
        [
            # nothing found in a named repo or user: it may be a fork, which
            # GitHub only searches with fork:true
            (
                "run_agent repo:haloooloolo/rocketwatch",
                [
                    "run_agent repo:haloooloolo/rocketwatch",
                    "run_agent repo:haloooloolo/rocketwatch fork:true",
                ],
            ),
            (
                "run_agent user:haloooloolo",
                ["run_agent user:haloooloolo", "run_agent user:haloooloolo fork:true"],
            ),
            ("x repo:a/b fork:false", ["x repo:a/b fork:false"]),
            ("SECONDS_PER_SLOT org:ethereum", ["SECONDS_PER_SLOT org:ethereum"]),
        ],
    )
    async def test_own_scope_replaces_default_org(
        self, monkeypatch: pytest.MonkeyPatch, query: str, sent: list[str]
    ) -> None:
        session = _http(monkeypatch, 200, {"total_count": 0, "items": []})
        await AskTools(MagicMock(), "token").github_search(
            GithubSearchInput(query=query)
        )
        assert [r["params"]["q"] for r in session.requests] == sent

    async def test_scoped_hits_skip_fork_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _http(monkeypatch, 200, {"total_count": 1, "items": [self.HIT]})
        await AskTools(MagicMock(), "token").github_search(
            GithubSearchInput(query="dequeue repo:rocket-pool/rocketpool")
        )
        assert [r["params"]["q"] for r in session.requests] == [
            "dequeue repo:rocket-pool/rocketpool"
        ]

    async def test_api_error_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _http(monkeypatch, 403, {"message": "rate limit exceeded"})
        with pytest.raises(ToolError, match="rate limit"):
            await AskTools(MagicMock(), "token").github_search(
                GithubSearchInput(query="dequeue")
            )


class TestRocketDash:
    async def test_gets_endpoint_with_network(
        self, tools: AskTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _http(monkeypatch, 200, {"history": [{"interval": 55}]})
        result = await tools.rocketdash_get(
            RocketDashInput(endpoint="/api/rewards/node", params={"address": NODE})
        )
        assert json.loads(result) == {"history": [{"interval": 55}]}
        [request] = session.requests
        assert request["url"] == "https://rocketdash.net/api/rewards/node"
        assert request["params"] == {"network": "mainnet", "address": NODE}

    @pytest.mark.parametrize(
        "endpoint",
        [
            "gov/admin/grant",
            "gov/vote",
            "gov/delete-proposal",
            "live/stream",
            "../admin",
            "rewards/node?address=x",
            "https://evil.example/api",
        ],
    )
    async def test_rejects_disallowed_endpoints(
        self, tools: AskTools, monkeypatch: pytest.MonkeyPatch, endpoint: str
    ) -> None:
        session = _http(monkeypatch, 200, {})
        with pytest.raises(ToolError, match="not an allowed endpoint"):
            await tools.rocketdash_get(RocketDashInput(endpoint=endpoint))
        assert session.requests == []

    async def test_error_page_is_summarized(
        self, tools: AskTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _http(
            monkeypatch,
            404,
            "<html><body><pre>Cannot GET /api/nope</pre></body></html>",
        )
        with pytest.raises(ToolError, match="404: Cannot GET /api/nope"):
            await tools.rocketdash_get(RocketDashInput(endpoint="nope"))


class TestBlockTime:
    @pytest.fixture(autouse=True)
    def chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def to_block(ts: int) -> int:
            return (ts - 1_600_000_000) // 12

        async def to_ts(block: int) -> int:
            return 1_600_000_000 + block * 12

        monkeypatch.setattr(tools_module, "ts_to_block", to_block)
        monkeypatch.setattr(tools_module, "block_to_ts", to_ts)

    async def test_date_to_block(self, tools: AskTools) -> None:
        result = json.loads(
            await tools.block_time(BlockTimeInput(timestamp="2024-01-01"))
        )
        assert result["block"] == (1_704_067_200 - 1_600_000_000) // 12
        assert abs(result["timestamp"] - 1_704_067_200) < 12

    async def test_block_to_time(self, tools: AskTools) -> None:
        result = json.loads(await tools.block_time(BlockTimeInput(block=100)))
        assert result == {
            "block": 100,
            "timestamp": 1_600_001_200,
            "utc": "2020-09-13T12:46:40+00:00",
        }

    @pytest.mark.parametrize(
        "args",
        [{}, {"timestamp": 1_700_000_000, "block": 1}, {"timestamp": 4_000_000_000}],
    )
    async def test_rejects_bad_input(
        self, tools: AskTools, args: dict[str, Any]
    ) -> None:
        with pytest.raises(ToolError):
            await tools.block_time(BlockTimeInput.model_validate(args))


class TestAddressInfo:
    @pytest.fixture(autouse=True)
    def lookups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        names = {NODE: "staic.eth"}
        monkeypatch.setattr(
            tools_module, "get_name", AsyncMock(side_effect=lambda a: names.get(a))
        )
        monkeypatch.setattr(
            tools_module,
            "resolve_name",
            AsyncMock(side_effect=lambda n: {v: k for k, v in names.items()}.get(n)),
        )
        monkeypatch.setattr(
            tools_module, "get_address_name", AsyncMock(return_value="Staic")
        )
        rp_stub = MagicMock()
        rp_stub.is_node = AsyncMock(return_value=True)
        rp_stub.is_minipool = AsyncMock(return_value=False)
        rp_stub.is_megapool = AsyncMock(return_value=False)
        rp_stub.get_name_by_address = MagicMock(return_value=None)
        monkeypatch.setattr(rocketpool.rp, "_instance", rp_stub)
        w3_stub = MagicMock()
        w3_stub.is_address = lambda a: a.startswith("0x")
        w3_stub.to_checksum_address = lambda a: a
        monkeypatch.setattr(shared_w3.w3, "_instance", w3_stub)

    @pytest.mark.parametrize("query", [NODE, "staic.eth"])
    async def test_identifies_node(self, tools: AskTools, query: str) -> None:
        info = json.loads(await tools.address_info(AddressInfoInput(address=query)))
        assert info == {
            "address": NODE,
            "ens": "staic.eth",
            "label": "Staic",
            "rocket_pool_contract": None,
            "node": True,
            "minipool": False,
            "megapool": False,
        }

    async def test_unknown_name(self, tools: AskTools) -> None:
        with pytest.raises(ToolError, match="not an address"):
            await tools.address_info(AddressInfoInput(address="nobody.eth"))


@pytest.fixture
def from_script() -> Any:
    token = agent._called_from_code.set(True)
    yield
    agent._called_from_code.reset(token)


class TestResultSize:
    async def test_direct_results_are_truncated(
        self, tools: AskTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _http(monkeypatch, 200, ["x" * 100] * 1000)
        result = await tools.defillama_get(DefiLlamaInput(host="api", path="protocols"))
        assert len(result) < MAX_RESULT_CHARS + 100
        assert "truncated" in result

    async def test_scripts_get_whole_results(
        self, tools: AskTools, monkeypatch: pytest.MonkeyPatch, from_script: None
    ) -> None:
        _http(monkeypatch, 200, ["x" * 100] * 1000)
        result = await tools.defillama_get(DefiLlamaInput(host="api", path="protocols"))
        assert len(json.loads(result)) == 1000

    async def test_scripts_get_an_error_past_their_limit(
        self, tools: AskTools, monkeypatch: pytest.MonkeyPatch, from_script: None
    ) -> None:
        _http(monkeypatch, 200, "x" * (MAX_SCRIPT_RESULT_CHARS + 1))
        with pytest.raises(ToolError, match="limit for scripts"):
            await tools.defillama_get(DefiLlamaInput(host="yields", path="pools"))


class TestDefiLlama:
    POOLS: ClassVar[dict[str, Any]] = {
        "status": "success",
        "data": [
            {"project": "lido", "chain": "Ethereum", "symbol": "STETH", "apy": 2.3},
            {
                "project": "rocket-pool",
                "chain": "Ethereum",
                "symbol": "RETH",
                "apy": 2.1,
            },
            {"project": "lido", "chain": "Arbitrum", "symbol": "WSTETH", "apy": 0.1},
        ],
    }

    async def test_requests_host_and_path(
        self, tools: AskTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _http(monkeypatch, 200, {"tvl": 1})
        await tools.defillama_get(
            DefiLlamaInput(
                host="coins",
                path="/prices/current/ethereum:0xae78736cd615f374d3085123a210448e74fc6393",
                params={"searchWidth": "4h"},
            )
        )
        [request] = session.requests
        assert request["url"] == (
            "https://coins.llama.fi/prices/current/"
            "ethereum:0xae78736cd615f374d3085123a210448e74fc6393"
        )
        assert request["params"] == {"searchWidth": "4h"}

    async def test_where_filters_data_list(
        self, tools: AskTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _http(monkeypatch, 200, self.POOLS)
        result = json.loads(
            await tools.defillama_get(
                DefiLlamaInput(
                    host="yields",
                    path="pools",
                    where={"project": "LIDO", "chain": "ethereum"},
                )
            )
        )
        assert [p["symbol"] for p in result["data"]] == ["STETH"]

    async def test_where_filters_top_level_list(
        self, tools: AskTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _http(monkeypatch, 200, self.POOLS["data"])
        result = json.loads(
            await tools.defillama_get(
                DefiLlamaInput(
                    host="api", path="protocols", where={"chain": "Arbitrum"}
                )
            )
        )
        assert [p["symbol"] for p in result] == ["WSTETH"]

    async def test_where_needs_a_list(
        self, tools: AskTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _http(monkeypatch, 200, {"tvl": 1})
        with pytest.raises(ToolError, match="list of objects"):
            await tools.defillama_get(
                DefiLlamaInput(host="api", path="tvl/rocket-pool", where={"a": "b"})
            )

    @pytest.mark.parametrize("path", ["../secret", "pools?x=1", "https://evil"])
    async def test_rejects_bad_paths(
        self, tools: AskTools, monkeypatch: pytest.MonkeyPatch, path: str
    ) -> None:
        session = _http(monkeypatch, 200, {})
        with pytest.raises(ToolError, match="not a valid path"):
            await tools.defillama_get(DefiLlamaInput(host="api", path=path))
        assert session.requests == []

    async def test_error_status(
        self, tools: AskTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _http(monkeypatch, 404, "Not Found")
        with pytest.raises(ToolError, match="404: Not Found"):
            await tools.defillama_get(DefiLlamaInput(host="api", path="protocol/nope"))


class TestReadOnly:
    @pytest.mark.parametrize(
        "pipeline",
        [
            [{"$out": "nodes_copy"}],
            [{"$merge": {"into": "nodes"}}],
            [{"$facet": {"a": [{"$out": "x"}]}}],
            [{"$lookup": {"from": "x", "pipeline": [{"$merge": "y"}], "as": "z"}}],
        ],
    )
    async def test_rejects_write_stages(
        self, tools: AskTools, pipeline: list[dict[str, Any]]
    ) -> None:
        with pytest.raises(ToolError, match="not allowed"):
            await tools.mongo_aggregate(
                MongoAggregateInput(collection="node_operators", pipeline=pipeline)
            )


@pytest.mark.integration_db
class TestMongo:
    @pytest.fixture
    async def db_tools(self, mongo_db: AsyncDatabase[dict[str, Any]]) -> AskTools:
        await mongo_db.node_operators.insert_many(
            [
                {
                    "address": f"0x{i:040x}",
                    "rpl": {"total_stake": float(i * 100)},
                    "registered": datetime(2024, 1, i + 1, tzinfo=UTC),
                    "smoothing_pool_registration": i % 2 == 0,
                }
                for i in range(5)
            ]
        )
        await mongo_db.node_operators.create_index("address")
        return AskTools(mongo_db)

    async def test_list_collections(self, db_tools: AskTools) -> None:
        listed = json.loads(await db_tools.mongo_list_collections(NoInput()))
        assert listed == {"node_operators": 5}

    async def test_describe_collection(self, db_tools: AskTools) -> None:
        described = json.loads(
            await db_tools.mongo_describe_collection(
                CollectionInput(collection="node_operators")
            )
        )
        assert described["documents"] == 5
        assert described["fields"]["rpl.total_stake"]["types"] == ["double"]
        assert described["fields"]["registered"]["types"] == ["date"]
        assert described["fields"]["rpl"]["types"] == ["object"]
        assert [["address", 1]] in described["indexes"]

    async def test_find_with_filter_sort_and_limit(self, db_tools: AskTools) -> None:
        found = json.loads(
            await db_tools.mongo_find(
                MongoFindInput(
                    collection="node_operators",
                    filter={"rpl.total_stake": {"$gte": 200}},
                    projection={"_id": 0, "rpl": 1},
                    sort={"rpl.total_stake": -1},
                    limit=2,
                )
            )
        )
        assert found == [
            {"rpl": {"total_stake": 400.0}},
            {"rpl": {"total_stake": 300.0}},
        ]

    async def test_find_accepts_extended_json_dates(self, db_tools: AskTools) -> None:
        found = json.loads(
            await db_tools.mongo_find(
                MongoFindInput(
                    collection="node_operators",
                    filter={"registered": {"$gte": {"$date": "2024-01-04T00:00:00Z"}}},
                )
            )
        )
        assert len(found) == 2

    async def test_aggregate(self, db_tools: AskTools) -> None:
        grouped = json.loads(
            await db_tools.mongo_aggregate(
                MongoAggregateInput(
                    collection="node_operators",
                    pipeline=[
                        {
                            "$group": {
                                "_id": "$smoothing_pool_registration",
                                "stake": {"$sum": "$rpl.total_stake"},
                            }
                        },
                        {"$sort": {"_id": 1}},
                    ],
                )
            )
        )
        assert grouped == [
            {"_id": False, "stake": 400.0},
            {"_id": True, "stake": 600.0},
        ]

    async def test_aggregate_caps_results(
        self, db_tools: AskTools, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        await mongo_db.many.insert_many([{"i": i} for i in range(MAX_DOCS + 5)])
        capped = json.loads(
            await db_tools.mongo_aggregate(
                MongoAggregateInput(collection="many", pipeline=[])
            )
        )
        assert len(capped["documents"]) == MAX_DOCS
        assert "note" in capped

    async def test_write_stage_leaves_data_untouched(
        self, db_tools: AskTools, mongo_db: AsyncDatabase[dict[str, Any]]
    ) -> None:
        with pytest.raises(ToolError):
            await db_tools.mongo_aggregate(
                MongoAggregateInput(
                    collection="node_operators", pipeline=[{"$out": "stolen"}]
                )
            )
        assert "stolen" not in await mongo_db.list_collection_names()

    async def test_unknown_collection(self, db_tools: AskTools) -> None:
        with pytest.raises(ToolError, match="Unknown collection"):
            await db_tools.mongo_find(MongoFindInput(collection="nope"))
