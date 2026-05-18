from hexbytes import HexBytes

from rocketwatch.utils.shared_w3 import w3
from tests.lib.event_log_script import EventLogScript, make_log

ADDR_A = "0x" + "AA" * 20
ADDR_B = "0x" + "BB" * 20
TOPIC_X = b"\x01" * 32
TOPIC_Y = b"\x02" * 32


def _seed_three(script: EventLogScript) -> None:
    script.add(make_log(address=ADDR_A, topics=[TOPIC_X], block_number=100))
    script.add(make_log(address=ADDR_B, topics=[TOPIC_X], block_number=200))
    script.add(make_log(address=ADDR_A, topics=[TOPIC_Y], block_number=300))


class TestMakeLog:
    def test_constructs_log_receipt_with_hexbytes_topics(self) -> None:
        log = make_log(address=ADDR_A, topics=[TOPIC_X], block_number=42)
        # Topics get wrapped in HexBytes so downstream `.hex()` calls work.
        assert log["topics"][0] == HexBytes(TOPIC_X)
        assert log["blockNumber"] == 42


class TestGetLogsFiltering:
    async def test_returns_all_when_filter_empty(self) -> None:
        script = EventLogScript()
        _seed_three(script)
        assert len(await script.get_logs({})) == 3

    async def test_address_filter_single(self) -> None:
        script = EventLogScript()
        _seed_three(script)
        out = await script.get_logs({"address": ADDR_A})
        assert len(out) == 2
        assert all(log["address"] == ADDR_A for log in out)

    async def test_address_filter_list(self) -> None:
        script = EventLogScript()
        _seed_three(script)
        out = await script.get_logs({"address": [ADDR_B]})
        assert len(out) == 1
        assert out[0]["address"] == ADDR_B

    async def test_topic_filter_single_topic(self) -> None:
        script = EventLogScript()
        _seed_three(script)
        # `topics` is a sequence of slots; a non-None slot matches the topic
        # at that position.
        out = await script.get_logs({"topics": [TOPIC_Y]})
        assert len(out) == 1
        assert out[0]["topics"][0] == HexBytes(TOPIC_Y)

    async def test_topic_filter_list_of_alternatives(self) -> None:
        # `[[TOPIC_X, TOPIC_Y]]` means slot 0 may match either.
        script = EventLogScript()
        _seed_three(script)
        out = await script.get_logs({"topics": [[TOPIC_X, TOPIC_Y]]})
        assert len(out) == 3

    async def test_topic_filter_none_slot_is_wildcard(self) -> None:
        script = EventLogScript()
        _seed_three(script)
        out = await script.get_logs({"topics": [None]})
        assert len(out) == 3

    async def test_block_range_inclusive(self) -> None:
        script = EventLogScript()
        _seed_three(script)
        out = await script.get_logs({"fromBlock": 100, "toBlock": 200})
        assert sorted(log["blockNumber"] for log in out) == [100, 200]

    async def test_from_block_excludes_below(self) -> None:
        script = EventLogScript()
        _seed_three(script)
        out = await script.get_logs({"fromBlock": 150})
        # 100 dropped; 200 and 300 retained.
        assert sorted(log["blockNumber"] for log in out) == [200, 300]

    async def test_latest_strings_are_treated_as_wildcard(self) -> None:
        # `toBlock="latest"` is a wildcard — no upper bound applied.
        script = EventLogScript()
        _seed_three(script)
        out = await script.get_logs({"toBlock": "latest"})
        assert len(out) == 3

    async def test_combined_address_topic_block_filter(self) -> None:
        script = EventLogScript()
        _seed_three(script)
        out = await script.get_logs(
            {
                "address": [ADDR_A],
                "topics": [TOPIC_X],
                "fromBlock": 100,
                "toBlock": 100,
            }
        )
        assert len(out) == 1
        assert out[0]["blockNumber"] == 100


class TestFixtureWiring:
    async def test_fixture_replaces_w3_eth_get_logs(
        self, event_log_script: EventLogScript
    ) -> None:
        # The fixture monkeypatches the proxy's `_instance` so `w3.eth.get_logs`
        # routes into our script. Add a log, query through the proxy, get it back.
        event_log_script.add(
            make_log(address=ADDR_A, topics=[TOPIC_X], block_number=42)
        )
        out = await w3.eth.get_logs({"address": ADDR_A})
        assert len(out) == 1
        assert out[0]["blockNumber"] == 42

    async def test_fixture_provides_async_block_number(
        self, event_log_script: EventLogScript
    ) -> None:
        # Plugins commonly call `w3.eth.get_block_number()` to anchor the
        # current head; the fixture stubs it with a large default so range
        # filters don't accidentally drop everything.
        head = await w3.eth.get_block_number()
        assert head > 10**6
