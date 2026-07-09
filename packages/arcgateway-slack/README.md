# arcgateway-slack

Slack (Socket Mode) platform adapter **plugin** for
[`arcgateway`](../arcgateway). All Slack-specific code lives here.

## Use it

```bash
pip install arcgateway arcgateway-slack
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_APP_TOKEN=xapp-...
```

```toml
# ~/.arc/gateway.toml
[platforms.slack]
enabled = true
bot_token_env = "SLACK_BOT_TOKEN"
app_token_env = "SLACK_APP_TOKEN"
allowed_user_ids = ["UABC123"]   # empty = deny all (fail-closed)
```

```bash
arcgateway start
```

## Layout

| Module | Responsibility |
|--------|----------------|
| `adapter.py` | `SlackAdapter` — Socket Mode, SQLite replay dedup, auth allowlist, audit. |
| `config.py` | `SlackPlatformConfig` — `[platforms.slack]` schema + token resolution. |
| `plugin.py` | `build(ctx)` + `PLUGIN` — registered under the `arcgateway.adapters` entry point. |

## License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
