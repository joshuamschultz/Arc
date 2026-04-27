# arcagent

Autonomous agent nucleus. Wraps arcrun and arcllm with persistent DID identity, TOML-driven configuration, skill discovery, extension sandboxing, session persistence, and an event-driven module bus.

## Layer position

arcagent depends on arcrun, arcllm, and arctrust. arcgateway depends on arcagent. arcagent never imports from them.

## What it provides

Public API from `arcagent.__init__`:

- `ArcAgentError` — base exception for all arcagent errors
- `ConfigError` — TOML configuration parse or validation failure
- `IdentityError` — DID or keypair problem during agent startup
- `IdentityRequired` — raised when an operation requires a DID that has not been provisioned
- `ToolError` — tool registry or tool execution failure
- `ToolVetoedError` — a module bus handler vetoed a tool call (extends `ToolError`)
- `ContextError` — context window management failure
- `ModuleBusError` — module bus dispatch failure

Core classes (import from `arcagent.core`):

- `ArcAgent` — the main agent class; requires a provisioned DID at construction; exposes `startup()`, `shutdown()`, `run(task)`, `chat(message)`, `reload()`; wires identity, tool registry, skill registry, extension loader, context manager, session store, and module bus
- `ArcAgentConfig` — Pydantic model for `arcagent.toml`; all config sections (agent, llm, identity, vault, tools, telemetry, context, session, extensions, modules)

Every operation emits arctrust audit events (Identity, Sign, Authorize, Audit — four pillars at all tiers per ADR-019).

## Quick example

```python
from arcagent.core.agent import ArcAgent
from arcagent.core.config import load_config

config = load_config("my-agent/arcagent.toml")
agent = ArcAgent(config, config_path="my-agent/arcagent.toml")

await agent.startup()
result = await agent.run("Summarize the files in /workspace/reports/")
print(result.content)
await agent.shutdown()
```

## Key behaviors

- DID is provisioned (or loaded from `key_dir`) during `startup()`; `IdentityRequired` is raised if construction proceeds without a resolvable DID
- Extensions are loaded from `workspace/extensions/` and `~/.arcagent/extensions/`; each runs in a configurable sandbox mode (`workspace`, `paths`, or `strict`)
- Skills are discovered from `workspace/skills/` and `~/.arcagent/skills/`; injected into the model's context window
- Sessions are persisted as JSONL transcripts in `workspace/sessions/`; loadable by session ID
- Context window management in three tiers: no action (<70%), observation masking (70–95%), emergency truncation (>95%)
- Module bus dispatches events with priority ordering (10=policy, 50=security, 100=default, 200=logging); any handler can veto a tool call; all handlers still execute for audit completeness

## Architecture references

- SPEC-017: Arc Core Hardening — 5-layer policy pipeline, dynamic tool safety, parallel tool dispatch
- ADR-019: Four Pillars Universal — DID required at all tiers; pairing signature universal (not Federal-only)
- SPEC-007: DID Identity Unification — `did:arc:{org}:{type}/{hash}` provisioning

## Status

- Tests: 3136+ (run with `uv run --no-sync pytest packages/arcagent/tests`)
- Coverage: core components >= 90%
- ruff + mypy --strict: active (concurrent cleanup in progress)
