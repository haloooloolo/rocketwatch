# Rocket Watch

[![Test](https://github.com/haloooloolo/rocketwatch/actions/workflows/test.yml/badge.svg)](https://github.com/haloooloolo/rocketwatch/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/haloooloolo/rocketwatch/graph/badge.svg)](https://codecov.io/gh/haloooloolo/rocketwatch)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)

A Discord bot that monitors and reports on [Rocket Pool](https://rocketpool.net) protocol activity across the Ethereum execution and consensus layers.

## Features

- **On-chain event tracking**: monitors Rocket Pool smart contract events (deposits, minipools, rewards, governance votes, etc.) and posts formatted embeds to Discord
- **Beacon chain integration**: tracks validator proposals, sync committees, and consensus layer activity
- **Governance monitoring**: follows on-chain DAO votes (pDAO, oDAO, Security Council) and Snapshot proposals
- **Data visualization**: generates APR charts, collateral distributions, fee breakdowns, and TVL calculations using matplotlib
- **ENS resolution**: resolves and caches ENS names for readable address display
- **Multi-channel support**: split event tracking and status messages across multiple channels
- **Deduplication**: prevents duplicate messages caused by chain reorgs or bot restarts
- **Dynamic contract loading**: retrieves contract addresses from the Rocket Pool storage contract at startup, automatically supporting protocol upgrades
- **Plugin system**: 40+ plugins that can be individually enabled or disabled

## Architecture

```
rocketwatch/
├── __main__.py              # Entry point
├── rocketwatch.py           # Bot class, plugin loader, error handling
├── config.toml.sample       # Configuration template
├── Dockerfile
├── plugins/                 # 40+ plugin modules
│   ├── event_core/          # Main event tracking logic
│   ├── dao/                 # On-chain governance
│   ├── snapshot/            # Off-chain governance
│   ├── apr/                 # APR calculations & charts
│   ├── rewards/             # Reward estimation
│   ├── tvl/                 # Total Value Locked
│   ├── proposals/           # Block proposals
│   └── ...
└── utils/
    ├── config.py            # Pydantic config models
    ├── rocketpool.py        # Contract interface with caching
    ├── shared_w3.py         # Web3 client instances
    ├── embeds.py            # Discord embed formatting
    ├── solidity.py          # Unit conversions
    ├── readable.py          # Human-readable formatting
    └── ...
```

## Prerequisites

- Python 3.12+
- MongoDB 8.x
- Ethereum execution and consensus layer RPC endpoints
- Discord bot token

## Setup

### Configuration

Copy the sample config and fill in your values:

```sh
cp rocketwatch/config.toml.sample rocketwatch/config.toml
```

Key configuration sections:

| Section | Purpose |
|---|---|
| `discord` | Bot token, owner/server IDs, channel mappings |
| `execution_layer` | RPC endpoints (current, mainnet, archive) and Etherscan API key |
| `consensus_layer` | Beacon API endpoint and beaconcha.in API key |
| `mongodb` | Database connection URI |
| `rocketpool` | Chain, contract addresses, DAO multisigs, support settings |
| `modules` | Plugin include/exclude lists |
| `events` | Event tracking setup |

### Docker (recommended)

```sh
docker compose up -d
```

This starts the bot, MongoDB, and [Watchtower](https://containrrr.dev/watchtower/) for automatic updates.

### Manual

```sh
# Install uv (https://docs.astral.sh/uv/)
cd rocketwatch
uv run .
```

## Development

### Linting

```sh
uv run ruff check rocketwatch/
```

Configured rules: `B` (bugbear), `E` (pycodestyle), `F` (pyflakes), `I` (isort), `RUF`, `SIM`, `UP` (pyupgrade), `W` (warnings).

### Type checking

```sh
uv run mypy rocketwatch/
```

### Testing

```sh
uv run --extra test pytest
```

### Plugin structure

Each plugin lives in `rocketwatch/plugins/<name>/` and follows this pattern:

```python
from discord.ext import commands

class MyPlugin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # slash commands, event listeners, background tasks, etc.

async def setup(bot):
    await bot.add_cog(MyPlugin(bot))
```

Plugins that track events extend `EventPlugin` from [`utils/event.py`](rocketwatch/utils/event.py) and implement the `_get_new_events()` method, which is called periodically to check for new events. They may also override `get_past_events()` to support querying historical events for a given block range:

```python
from rocketwatch.utils.event import Event, EventPlugin
from rocketwatch.utils.embeds import Embed

class MyEventPlugin(EventPlugin):
    async def _get_new_events(self) -> list[Event]:
        events = []
        # query contracts, APIs, etc.
        embed = Embed(title="My Event")
        events.append(Event(
            embed=embed,
            topic="my_topic",
            event_name="my_event",
            unique_id="some_unique_id",
            block_number=block_number,
        ))
        return events
```

Plugins that provide a rotating status embed (displayed by the bot when idle) extend `StatusPlugin` from [`utils/status.py`](rocketwatch/utils/status.py) and implement the `get_status()` method:

```python
from rocketwatch.utils.status import StatusPlugin
from rocketwatch.utils.embeds import Embed

class MyStatusPlugin(StatusPlugin):
    async def get_status(self) -> Embed:
        embed = Embed(title="My Status")
        embed.add_field(name="Info", value="...")
        return embed
```

Plugins can be selectively loaded via the `modules.include` / `modules.exclude` config fields.

## Sentinel

[Sentinel](sentinel/) is a companion moderation bot that acts as a privilege escalation service for Rocket Watch. It exposes an authenticated HTTP API with per-key guardrails, so Rocket Watch itself doesn't need elevated Discord permissions.

See the [Sentinel README](sentinel/README.md) for setup and API documentation.

## CI/CD

| Workflow | Trigger | Purpose |
|---|---|---|
| [Lint](.github/workflows/lint.yml) | Push & PR to main | Ruff linting & mypy type checking |
| [Test](.github/workflows/test.yml) | Push & PR to main | pytest suite |
| [Build](.github/workflows/build.yml) | Push to main | Build & push image to DockerHub |

## License

[GNU General Public License v3](LICENSE)
