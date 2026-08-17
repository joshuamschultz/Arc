# arcrun

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Async ReAct execution engine: think → act → observe with tool registry, sandbox, streaming, strategies, and hash-chained events. **No agent identity, skills, sessions, or memory.**

## Layer

**Loop.** Depends on `arcllm`, `arctrust`, `arcstore`, and `arcprompt`. It has no knowledge of concrete callers or higher layers.

Must not import any higher layer (`tests/test_layering.py`, architecture guards).

## Layout

```
src/arcrun/
  loop.py           # Core run / run_async / run_stream
  executor.py
  registry.py       # ToolRegistry
  events.py         # EventBus, verify_chain
  streams.py
  sandbox.py
  capabilities.py   # CapabilityProvider Protocol (implementations live in hosts)
  checkpoint.py
  prompts.py
  strategies/       # Strategy selection (react, code, dynamic)
  dynamic/          # Dynamic strategy internals: host boundary, grammar, interpreter, journal
  builtins/         # Loop-level builtins
  backends/         # docker, firecracker, spawn, …
  context/          # Stock system-prompt markdown
```

## Entry points

`run` / `run_async` / `run_stream`, `RunHandle`, `ToolRegistry`, `Tool`/`ToolContext`, `EventBus`, `Strategy` (react / code / dynamic), sandbox helpers, stream event types, `SystemPrompt`, `dynamic.ScriptHost`.

## Package rules

- Own the model-execution seam used by higher layers while keeping provider routing in ArcLLM.
- Consume ArcLLM with `import arcllm`; consumers likewise use only `import arcrun` and qualified root-facade names.
- `CapabilityProvider` is a Protocol; concrete providers belong in the host.
- Stock prompts under `arcrun/context/`; overlays via `arcprompt`.
- Sandbox / backend work stays defensive (path traversal, injection) — see `tests/security/` and integration docker/firecracker tests.

## Tests

`packages/arcrun/tests/` — unit (loop/strategy/sandbox), `integration/` (docker/firecracker/spawn), `security/`.

## Working here

Loop changes touch the hottest path in the stack — keep concerns pure. Prefer extending `Strategy` / registry / stream contracts over special-casing agent behavior.

`dynamic/grammar.py`'s whitelist is a security boundary, not a style choice — it is the only thing standing between a model-authored script and arbitrary code (LLM01/ASI05). Every effect a script can have is named on `dynamic/host.py`'s `ScriptHost` Protocol; widening the grammar (a new builtin, a new method, attribute access) means auditing what that name newly reaches, the same care as reviewing a sandbox escape.
