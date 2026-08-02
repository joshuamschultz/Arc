# arcllm

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Provider-agnostic LLM client: many providers over direct `httpx` (no vendor SDKs), with PII redaction, signing hooks, OTel, and audit.

## Layer

**Provider abstraction.** Depends on `arctrust`, `arcstore`. Imported by `arcrun`, `arcagent`, `arcmemory`, `arccli`, `arcui`.

**Concern boundary:** all LLM calls live here. No agent state, no ReAct loop.

## Layout

```
src/arcllm/
  registry.py      # load_model / provider registry — stays in arcllm
  types.py         # Message, Tool, LLMResponse, StreamEvent — shared contract
  config.py
  embeddings.py
  adapters/        # httpx-shaped provider adapters
  providers/
  modules/         # retry, fallback, guardrails, injection, rate_limit, otel, …
  backends/
  trace_store.py
  vault.py
```

## Entry points

Eager: `load_model`, message/tool/response types, config loaders, exceptions.  
Lazy (`__getattr__`): adapters, modules, embeddings (`embed`, `resolve_embedder`), trace store.

## Package rules

- **No vendor LLM SDKs** — httpx only (root CLAUDE.md / tech.md).
- `arcrun` must **not** call `load_model` or import `arcllm.registry` (architecture test). Receive a pre-built model instead.
- Prefer `arcllm.types` as the cross-package contract (e.g. `StreamEvent` for SPEC-059).
- Package `.env` must not inject API keys into unrelated consumers; respect cwd/.env conventions.

## Tests

`packages/arcllm/tests/` (+ `tests/security/`) — providers, modules, PII, embeddings, spool, import isolation.

## Working here

New provider = adapter under `adapters/`/`providers/` matching existing httpx patterns. Streaming: implement the shared stream contract; keep an unbreakable single-event fallback on the base path.
