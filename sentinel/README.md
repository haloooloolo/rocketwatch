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

The optional `[api.defaults]` section sets guardrail values inherited by all keys. Each `[[api.keys]]` entry only needs to specify overrides. If a value is not set in either place, the action is unrestricted (no age/duration limit).

| Key | Description |
|-----|-------------|
| `delete_message_max_age` | Allow deleting messages younger than this many seconds; 0 to disable deletion; omit for no limit |
| `lock_thread_max_age` | Allow locking threads younger than this many seconds; 0 to disable locking; omit for no limit |
| `delete_thread_max_age` | Allow deleting threads younger than this many seconds; 0 to disable thread deletion; omit for no limit |
| `timeout_member_max_duration` | Maximum duration of user timeouts in seconds; 0 to disable timeouts; omit for no limit |
| `kick_member_max_age` | Allow kicking members who joined less than this many seconds ago; 0 to disable kicks; omit for no limit |
| `ban_member_max_age` | Allow banning members who joined less than this many seconds ago; 0 to disable bans; omit for no limit |
| `max_actions_per_hour` | Rate limit for this key; omit for no limit |

Each `[[api.keys]]` entry defines a key:

| Key | Description |
|-----|-------------|
| `secret` | The API key — Rocket Watch's `sentinel.api_key` must match one of these |
| `allowed_server_ids` | Discord server IDs this key is allowed to act in |
| *(guardrail fields)* | Any of the above — overrides the `[api.defaults]` value for this key only |

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

Guardrails: server must be in the allowlist, message must be younger than `delete_message_max_age`.

### `POST /lock_thread`

```json
{"guild_id": 123, "thread_id": 456, "reason": "..."}
```

Locks and archives the thread, preventing non-moderators from posting or unarchiving. Guardrails: server must be in the allowlist, thread must be younger than `lock_thread_max_age`.

### `POST /delete_thread`

```json
{"guild_id": 123, "thread_id": 456, "reason": "..."}
```

Deletes the thread entirely. Guardrails: server must be in the allowlist, thread must be younger than `delete_thread_max_age`.

### `POST /timeout_member`

```json
{"guild_id": 123, "user_id": 456, "duration_seconds": 600, "reason": "..."}
```

Guardrails: server allowlist, duration capped at `timeout_member_max_duration`, refuses to timeout moderators.

### `POST /kick_member`

```json
{"guild_id": 123, "user_id": 456, "reason": "..."}
```

Guardrails: server allowlist, refuses to kick moderators, member must have joined less than `kick_member_max_age` ago; 0 to disable.

### `POST /ban_member`

```json
{"guild_id": 123, "user_id": 456, "reason": "..."}
```

Guardrails: server allowlist, refuses to ban moderators, member must have joined less than `ban_member_max_age` ago; 0 to disable.

### `POST /is_banned`

```json
{"guild_id": 123, "user_id": 456}
```

Returns `{"banned": true, "reason": "...", "user_id": 456}` if the user is banned, or `{"banned": false, "user_id": 456}` otherwise. Guardrails: server allowlist only.

### `POST /is_timed_out`

```json
{"guild_id": 123, "user_id": 456}
```

Returns `{"timed_out": true, "until": "...", "user_id": 456}` if the member is currently timed out, or `{"timed_out": false, "user_id": 456}` otherwise. Returns 404 if the member is not in the server. Guardrails: server allowlist only.

### Error responses

| Status | Meaning |
|--------|---------|
| 401 | Invalid or missing API key |
| 403 | Sentinel lacks the required Discord permission, or action is disabled in config |
| 404 | Server, channel, member, or message not found |
| 409 | Member is already timed out |
| 422 | Guardrail violation (message too old, thread/member too old or age unknown, duration too long, server not allowed, target is moderator) |
| 429 | Rate limited — `retry_after_seconds` included in response |

## Audit

All actions are logged to stdout. Docker persists these via the json-file log driver. Discord's built-in audit log also records message deletions and timeouts performed by the bot.
