# arcgateway-telegram

Telegram platform adapter **plugin** for [`arcgateway`](../arcgateway). All
Telegram-specific code lives here — the gateway core names no platform.

## How it plugs in

The package registers an `AdapterPlugin` under the `arcgateway.adapters`
entry-point group:

```toml
[project.entry-points."arcgateway.adapters"]
telegram = "arcgateway_telegram:PLUGIN"
```

At startup the gateway iterates that group, applies its Authorize/Audit gate
(`arcgateway.adapters.registry`), and — for an enabled `[platforms.telegram]`
block — calls `arcgateway_telegram.plugin.build(ctx)` to construct the adapter.

## Use it

```bash
pip install arcgateway arcgateway-telegram
export TELEGRAM_BOT_TOKEN=123456:abc...
```

```toml
# ~/.arc/gateway.toml
[gateway]
agent_did = "did:arc:agent:default"

[platforms.telegram]
enabled = true
token_env = "TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123456789]   # empty = deny all (fail-closed)
# agent_did = "..."              # optional per-platform override
```

```bash
arc gateway start
```

DM the bot, run `arc gateway pair` to approve your user, and chat with the agent.

## Layout

| Module | Responsibility |
|--------|----------------|
| `adapter.py` | `TelegramAdapter` — long-poll, auth allowlist, reconnect/backoff, message splitting, audit. Implements `arcgateway.adapters.base.BasePlatformAdapter`. |
| `config.py` | `TelegramPlatformConfig` — the `[platforms.telegram]` Pydantic schema + token resolution. |
| `plugin.py` | `build(ctx)` + `PLUGIN` — validates config, resolves the token (raising `AdapterUnavailable` if absent), constructs the adapter. |

## License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
