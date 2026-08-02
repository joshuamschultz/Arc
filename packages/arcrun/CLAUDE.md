# arcrun

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Async ReAct execution engine: think → act → observe with tool registry, sandbox, streaming, strategies, and hash-chained events. **No agent identity, skills, sessions, or memory.**

## Layer

**Loop.** Depends on `arcllm`, `arctrust`, `arcstore`, `arcprompt`. Imported by `arcagent` (primary), `arccli`, and `arcmemory` only via `react_adapter.py`.

Must **not** import `arcagent`, `arcui`, `arccli`, `arcgateway`, `arcteam` (`tests/test_layering.py`, architecture guards). Must **not** call `arcllm.load_model` / import `arcllm.registry`.

## Layout

```
src/arcrun/
  loop.py           # Core run / run_async / run_stream
  executor.py
  registry.py       # ToolRegistry
  events.py         # EventBus, verify_chain
  streams.py
  sandbox.py
  capabilities.py   # CapabilityProvider Protocol (impls live in arcagent)
  checkpoint.py
  prompts.py
  strategies/       # Strategy selection (reactive, etc.)
  builtins/         # Loop-level builtins
  backends/         # docker, firecracker, spawn, …
  context/          # Stock system-prompt markdown
```

## Entry points

`run` / `run_async` / `run_stream`, `RunHandle`, `ToolRegistry`, `Tool`/`ToolContext`, `EventBus`, `Strategy`, sandbox helpers, stream event types, `SystemPrompt`.

## Package rules

- Receive a **pre-built** model + tools; drive the loop — do not construct providers here.
- `CapabilityProvider` is a Protocol; concrete providers belong in `arcagent`.
- Stock prompts under `arcrun/context/`; overlays via `arcprompt`.
- Sandbox / backend work stays defensive (path traversal, injection) — see `tests/security/` and integration docker/firecracker tests.

## Tests

`packages/arcrun/tests/` — unit (loop/strategy/sandbox), `integration/` (docker/firecracker/spawn), `security/`.

## Working here

Loop changes touch the hottest path in the stack — keep concerns pure. Prefer extending `Strategy` / registry / stream contracts over special-casing agent behavior.
