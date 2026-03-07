# Rocket Watch

A Discord bot that monitors and reports on [Rocket Pool](https://rocketpool.net) protocol activity across the Ethereum execution and consensus layers.

## Features

- **On-chain event tracking** — monitors Rocket Pool smart contract events (deposits, minipools, rewards, governance votes, etc.) and posts formatted embeds to Discord
- **Beacon chain integration** — tracks validator proposals, sync committees, and consensus layer activity
- **Governance monitoring** — follows on-chain DAO votes (pDAO, oDAO, Security Council) and Snapshot proposals
- **Data visualization** — generates APR charts, collateral distributions, fee breakdowns, and TVL calculations using matplotlib
- **ENS resolution** — resolves and caches ENS names for readable address display
- **Multi-channel support** — split event tracking and status messages across multiple channels
- **Deduplication** — prevents duplicate messages caused by chain reorgs or bot restarts
- **Dynamic contract loading** — retrieves contract addresses from the Rocket Pool storage contract at startup, automatically supporting protocol upgrades
- **Plugin system** — 40+ plugins that can be individually enabled or disabled

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

Plugins that track on-chain events extend `EventPlugin` from `utils/event.py`. Plugins can be selectively loaded via the `modules.include` / `modules.exclude` config fields.

## CI/CD

| Workflow | Trigger | Purpose |
|---|---|---|
| [Lint](.github/workflows/lint.yml) | Push & PR to main | Ruff linting |
| [Test](.github/workflows/test.yml) | Push & PR to main | pytest suite |
| [Docker CI](.github/workflows/docker-ci.yml) | Push to main | Build & push image to DockerHub |

## License

[GNU General Public License v3](LICENSE)
