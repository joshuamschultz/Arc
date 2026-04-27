# arcgateway

Long-running daemon that makes ArcAgents reachable from any chat platform (Telegram, Slack, Discord) with multi-agent session routing and operator pairing controls.

## Layer position

arcgateway depends on arcagent, arcrun, arcllm, and arctrust. No other Arc package depends on arcgateway. It is a terminal node in the dependency graph.

## What it provides

- `GatewayRunner` — supervises N platform adapters inside an `asyncio.TaskGroup`; a crash in one adapter never kills siblings; routes inbound messages to `SessionRouter`
- `SessionRouter`, `build_session_key` — per-(user, agent) session management with a synchronous pre-await guard guaranteeing exactly one agent task per session key regardless of concurrent inbound messages
- `InboundEvent` — normalized event from any platform adapter; carries platform, user ID hash, chat ID, and message content
- `Delta` — streamed response chunk forwarded from the executor back to the platform adapter
- `Executor` (Protocol), `AsyncioExecutor` — pluggable execution; `AsyncioExecutor` runs the agent in-process (personal/enterprise tier)
- `DeliveryTarget` — parsed `platform:chat_id[:thread_id]` address

All pairing operations emit arctrust audit events. Pairing requires operator approval before a user hash is added to the session allowlist.

## Quick example

```python
from arcgateway import GatewayRunner, AsyncioExecutor

executor = AsyncioExecutor(agent_config_path="my-agent/arcagent.toml")
runner = GatewayRunner(executor=executor)
await runner.run()  # blocks; handle SIGINT/SIGTERM externally
```

## Pairing controls

Operators approve DM pairings via CLI (see `docs/cli.md`):

```bash
arc gateway pair list              # show pending codes
arc gateway pair approve <code>    # approve a code
arc gateway pair revoke <code>     # revoke a code
```

## Architecture references

- SPEC-016: Multi-Agent UI — gateway integrates with arcui via UIBridgeSink
- SPEC-018: Hermes Parity — session race-condition guard (synchronous pre-await)
- ADR-019: Four Pillars Universal — pairing always-sign enforced at all tiers

## Status

- Tests: 494 (run with `uv run --no-sync pytest packages/arcgateway/tests`)
- Coverage: 94%
- ruff + mypy --strict: clean
