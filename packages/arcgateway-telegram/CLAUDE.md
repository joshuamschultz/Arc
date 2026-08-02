# arcgateway-telegram

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Telegram platform adapter **plugin** for `arcgateway`. All Telegram-specific code lives here — not in gateway core.

## Layer

Plugin package. Depends on `arcgateway`, `python-telegram-bot`. Discovered at runtime via entry point `arcgateway.adapters` → `telegram = "arcgateway_telegram:PLUGIN"`. Not hard-imported by core.

## Layout

```
src/arcgateway_telegram/
  adapter.py    # TelegramAdapter
  config.py     # TelegramPlatformConfig
  plugin.py     # PLUGIN, build(ctx)
```

## Entry points

`PLUGIN`, `build`, `TelegramAdapter`, `TelegramPlatformConfig`, `split_message`.

## Package rules

- Empty `allowed_user_ids` = **deny all** (fail-closed).
- Missing token → `AdapterUnavailableError` (skip personal/enterprise; fail federal as configured).
- Do not push Telegram SDK usage into `arcgateway` core.

## Tests

`packages/arcgateway-telegram/tests/` — adapter, streaming, polling conflict, pairing e2e, plugin.

## Working here

Mirror sibling plugin shape (`slack`, `mattermost`). Validate `[platforms.telegram]` inside `build(ctx)`.
