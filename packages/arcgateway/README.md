# arcgateway

Long-running daemon that makes ArcAgents reachable from any chat platform.

## Overview

arcgateway is a single asyncio process that:

1. Supervises N platform adapters (Telegram, Slack, Discord, etc.) inside an `asyncio.TaskGroup` so a crash in one adapter never kills siblings.
2. Routes incoming messages to per-(user, agent) `SessionRouter` instances with a **synchronous pre-await guard** (Hermes PR #4926) that guarantees exactly one agent task per session key regardless of concurrent inbound messages.
3. Dispatches agent execution via a pluggable `Executor` Protocol: `AsyncioExecutor` for personal/enterprise (in-process) and `SubprocessExecutor` for federal-tier (isolated child process).
4. Returns streamed `Delta` responses through `StreamBridge` to `Adapter.send()`.

## Architecture

```
                ┌──────────────────────────────┐
                │        arcgateway daemon       │
                │                                │
   Telegram ───┤  Adapter   ┐                    │
   Slack ──────┤  Adapter   ├──> SessionRouter ──┤──> Executor.run(event)
   Discord ────┤  Adapter   │                    │
                │            ┘                    │
                └──────────────────────────────────┘
```

## Package Boundaries

- `arcgateway` depends on `arc-agent` (it wraps/runs agents).
- `arc-agent` has **zero** knowledge of `arcgateway` — this is a one-way dependency.
- Platform SDK dependencies (slack-sdk, python-telegram-bot, etc.) are optional extras added in T1.7.

## Installation (Development)

**IMPORTANT:** Always install all Arc packages together using editable mode.
Installing packages piecemeal causes venv-drift — import errors from packages
that are not linked (issue surfaced during SPEC-018 M1 implementation).

Canonical setup command:

```bash
uv pip install \
    -e packages/arcgateway \
    -e packages/arccli \
    -e packages/arcagent \
    -e packages/arcllm \
    -e packages/arcrun
```

Or using the Makefile shortcut:

```bash
make install
```

This installs all five packages in editable mode so local source changes are
reflected immediately without re-installing.

## Getting Started

```toml
# ~/.arc/gateway.toml (personal tier)
[gateway]
tier = "personal"

[[gateway.adapters]]
platform = "telegram"
token_env = "TELEGRAM_BOT_TOKEN"
```

```bash
arcgateway start
```

## Security

All adapters run in isolated `asyncio.TaskGroup` tasks. Federal-tier deployments use `SubprocessExecutor` for full process isolation per agent session, with resource limits via `resource.setrlimit`. Platform credentials are never written to disk at federal/enterprise tiers — resolved via Vault Protocol at runtime.

## Status

T1.4 skeleton: GatewayRunner, BasePlatformAdapter Protocol, SessionRouter (with race-condition fix), Executor Protocol + AsyncioExecutor, DeliveryTarget parser, CLI stub.

Platform adapters (T1.7), SubprocessExecutor (T1.6), and StreamBridge flood-control (T1.6+) are pending.
