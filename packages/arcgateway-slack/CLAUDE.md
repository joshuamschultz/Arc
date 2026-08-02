# arcgateway-slack

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Slack Socket Mode adapter **plugin** for `arcgateway`. All Slack-specific code lives here.

## Layer

Plugin package. Depends on `arcgateway`, `slack-bolt`, `slack-sdk`. Entry point: `arcgateway.adapters` → `slack = "arcgateway_slack:PLUGIN"`.

## Layout

```
src/arcgateway_slack/
  adapter.py
  config.py
  plugin.py
```

## Entry points

`PLUGIN`, `build`, `SlackAdapter`, `SlackPlatformConfig`, `split_message`.

## Package rules

- Fail-closed empty allowlist.
- Needs `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`.
- Socket Mode + replay dedup (SQLite) are Slack responsibilities in **this** package only.
- Token no-leak: keep secrets out of logs/errors (see tests).

## Tests

`packages/arcgateway-slack/tests/` — adapter, dual-adapter chat, socket replay dedup, command injection, token leak.

## Working here

Same plugin contract as Telegram/Mattermost. Keep Socket Mode / dedup logic out of gateway core.
