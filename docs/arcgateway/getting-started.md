# Getting Started with arcgateway

arcgateway connects ArcAgent to real-time messaging platforms (Telegram, Slack) via a single long-running daemon with per-(user, agent) session routing and tier-aware execution isolation.

## Install

```bash
pip install arcgateway arccmd
```

Dev workspace (editable, all sibling packages):

```bash
uv pip install -e packages/arcgateway -e packages/arccli -e packages/arcagent -e packages/arcllm -e packages/arcrun
```

## Minimal config

Write `~/.arc/gateway.toml`:

```toml
[gateway]
tier = "personal"                     # personal | enterprise | federal
runtime_dir = "~/.arc/gateway"

[security]
tier = "personal"

[platforms.telegram]
enabled = true
bot_token_env_var = "TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123456789]

[platforms.slack]
enabled = false
bot_token_env_var = "SLACK_BOT_TOKEN"
app_token_env_var = "SLACK_APP_TOKEN"
allowed_user_ids = ["U0ABC123"]

[pairing]
enabled = true
```

The `GatewayConfig` Pydantic model (see `arcgateway/config.py`) loads this via `GatewayConfig.from_toml(path)`.

## Telegram

1. Create a bot via [@BotFather](https://t.me/BotFather) → `/newbot` → save token
2. `export TELEGRAM_BOT_TOKEN="123456789:ABCdef..."`
3. Add your Telegram user id to `allowed_user_ids` (send `/start` to your bot, it won't reply yet — check Telegram logs or use `@userinfobot`)
4. `arc gateway start`

## Slack

1. Create app at [api.slack.com/apps](https://api.slack.com/apps) → enable Socket Mode
2. Generate app-level token with `connections:write` scope (`xapp-...`)
3. Add bot scopes: `chat:write`, `im:history`, `im:read`; install; copy `xoxb-...` bot token
4. Set both env vars + add your Slack user id to `allowed_user_ids`
5. `arc gateway start`

## First-time pairing

Unknown users must pair before the agent responds. When you DM the bot, you get an 8-char code (e.g. `X7K2MQJP`). As operator, in another terminal:

```bash
arc gateway pair approve X7K2MQJP
```

Codes expire in 1h. Max 3 pending per platform. 5 failed approvals → 1h platform lockout. Alphabet is 32 unambiguous chars (no `0/O/1/I`). Implementation: `arcgateway/pairing.py::PairingStore`.

## Common pitfalls

- **One bot token per gateway instance.** `TelegramAdapter` uses long polling. Two instances on the same token silently lose updates to the race. See [multi-instance.md](./multi-instance.md).
- **Tokens out of source.** Use `bot_token_env_var`, never a literal token in `gateway.toml`. Federal tier requires vault-backed credentials; see [security.md](./security.md).
- **`allowed_user_ids` empty = deny all.** Fail-closed default (auth check in `adapters/telegram.py::TelegramAdapter`).

## CLI reference

```bash
arc gateway setup                         # write starter gateway.toml
arc gateway start                         # start daemon (blocks)
arc gateway stop --runtime-dir ~/.arc     # SIGTERM via PID file
arc gateway status --runtime-dir ~/.arc   # health check
arc gateway pair approve <CODE>           # approve pending pairing
arc gateway pair list                     # list pending
arc gateway pair revoke <CODE>            # revoke
```

All commands dispatch through `arccli.commands.registry` (centralized `CommandDef` list — one source of truth for CLI + gateway + platform-menu generators).

## Next

- [security.md](./security.md) — tier matrix, vault, subprocess isolation, audit events, NIST controls
- [multi-instance.md](./multi-instance.md) — horizontal scaling plan
