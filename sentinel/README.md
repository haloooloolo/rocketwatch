# Rocket Watch Sentinel

A lightweight moderation companion bot that acts as a privilege escalation service for [Rocket Watch](../rocketwatch). It expects `manage_messages`, `manage_threads`, `moderate_members`, `kick_members`, and `ban_members` permissions and exposes an authenticated HTTP API with configurable guardrails.

## Setup

### 1. Create a Discord bot

1. Create a new application at https://discord.com/developers/applications
2. Under **Bot**, enable the **Server Members** privileged intent
3. Under **Installation > Install Link**, configure the default permissions:
   - Manage Messages
   - Manage Threads
   - Moderate Members
   - Kick Members
   - Ban Members
4. Invite the bot to your server

### 2. Configure

Copy the sample config and fill in the values:

```bash
cp config.toml.sample config.toml
```

Generate an API key:

```bash
openssl rand -base64 32
```

| Key | Default | Description |
|-----|---------|-------------|
| `discord.token` | | Bot token from the Discord developer portal |
| `api.host` | `0.0.0.0` | Bind address for the HTTP server |
| `api.port` | `8080` | Port for the HTTP server |

Each `[[api.keys]]` entry defines a key with its own guardrails:

| Key | Default | Description |
|-----|---------|-------------|
| `secret` | | The API key — Rocket Watch's `sentinel.api_key` must match one of these |
| `allowed_server_ids` | `[]` | Discord server IDs this key is allowed to act in |
| `max_message_age_seconds` | `900` | Allow deleting messages younger than this; 0 to disable deletion |
| `max_thread_age_seconds` | `3600` | Allow locking threads younger than this; 0 to disable locking |
| `max_timeout_seconds` | `86400` | Maximum duration of user timeouts; 0 to disable timeouts |
| `allow_kick` | `false` | Enable the kick endpoint for this key |
| `allow_ban` | `false` | Enable the ban endpoint for this key |
| `max_actions_per_hour` | `100` | Rate limit for this key |

### 3. Deploy

```bash
docker compose up -d
```

Put Sentinel behind a reverse proxy (e.g., Caddy, nginx) with TLS so Rocket Watch can reach it over HTTPS.

### 4. Connect Rocket Watch

In Rocket Watch's `config.toml`:

```toml
[sentinel]
api_url = "https://sentinel.example.com"
api_key = "same-secret-as-sentinel"
```

## API

All endpoints require an `X-Api-Key` header matching a configured key. Guardrails are enforced per-key.

### `POST /delete_message`

```json
{"guild_id": 123, "channel_id": 456, "message_id": 789, "reason": "..."}
```

Guardrails: server must be in the allowlist, message must be younger than `max_message_age_seconds`.

### `POST /lock_thread`

```json
{"guild_id": 123, "thread_id": 456, "reason": "..."}
```

Locks and archives the thread, preventing non-moderators from posting or unarchiving. Guardrails: server must be in the allowlist, thread must be younger than `max_thread_age_seconds`.

### `POST /timeout_member`

```json
{"guild_id": 123, "user_id": 456, "duration_seconds": 600, "reason": "..."}
```

Guardrails: server allowlist, duration capped at `max_timeout_seconds`, refuses to timeout moderators.

### `POST /kick_member`

```json
{"guild_id": 123, "user_id": 456, "reason": "..."}
```

Requires `allow_kick = true` in config. Guardrails: server allowlist, refuses to kick moderators.

### `POST /ban_member`

```json
{"guild_id": 123, "user_id": 456, "reason": "..."}
```

Requires `allow_ban = true` in config. Guardrails: server allowlist, refuses to ban moderators.

### Error responses

| Status | Meaning |
|--------|---------|
| 401 | Invalid or missing API key |
| 403 | Sentinel lacks the required Discord permission, or action is disabled in config |
| 404 | Server, channel, member, or message not found |
| 409 | Member is already timed out |
| 422 | Guardrail violation (message too old, duration too long, server not allowed, target is moderator) |
| 429 | Rate limited — `retry_after_seconds` included in response |

## Audit

All actions are logged to stdout. Docker persists these via the json-file log driver. Discord's built-in audit log also records message deletions and timeouts performed by the bot.
