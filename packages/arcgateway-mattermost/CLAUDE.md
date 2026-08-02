# arcgateway-mattermost

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Mattermost adapter **plugin** for air-gapped DOE/lab chat (FedRAMP High / IL5 / JWICS-oriented deployments). All Mattermost-specific code lives here.

## Layer

Plugin package. Depends on `arcgateway`, `aiohttp`. Entry point: `arcgateway.adapters` → `mattermost = "arcgateway_mattermost:PLUGIN"`.

## Layout

```
src/arcgateway_mattermost/
  adapter.py    # WebSocket inbound + REST outbound
  config.py
  plugin.py
```

## Entry points

`PLUGIN`, `build`, `MattermostAdapter`, `MattermostPlatformConfig`.

## Package rules

- At `tier = "federal"`, refuse start if `server_url` is public (air-gap guard). `intranet_domains` is exact-hostname override.
- Empty `allowed_channel_ids` ⇒ DMs only.
- Federal air-gap logic belongs **here**, not in gateway core.
- Token no-leak: keep secrets out of logs/errors.

## Tests

`packages/arcgateway-mattermost/tests/` — adapter, integration, token no-leak, plugin.

## Working here

Same plugin contract as Telegram/Slack. Prefer tightening air-gap checks in this adapter over special-casing Mattermost in `arcgateway`.
