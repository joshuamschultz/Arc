# arcgateway-mattermost

Mattermost platform adapter **plugin** for [`arcgateway`](../arcgateway) — the
air-gapped DOE/National Lab chat surface (FedRAMP High / IL5 / JWICS). All
Mattermost-specific code lives here.

## Use it

```bash
pip install arcgateway arcgateway-mattermost
export MM_BOT_TOKEN=...
```

```toml
# ~/.arc/gateway.toml
[platforms.mattermost]
enabled = true
server_url = "https://mattermost.internal.example.gov"
bot_token_env = "MM_BOT_TOKEN"
allowed_channel_ids = ["channelid1"]     # empty = DMs only
intranet_domains = ["mattermost.internal.example.gov"]
```

At `tier = "federal"` the adapter refuses to start if `server_url` resolves to a
public address (air-gap guard).

## Layout

| Module | Responsibility |
|--------|----------------|
| `adapter.py` | `MattermostAdapter` — WebSocket inbound + REST outbound, per-channel queues, dedup, federal air-gap guard, audit. |
| `config.py` | `MattermostPlatformConfig` — `[platforms.mattermost]` schema + token resolution. |
| `plugin.py` | `build(ctx)` + `PLUGIN` — registered under the `arcgateway.adapters` entry point. |

## License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
