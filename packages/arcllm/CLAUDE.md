# arcllm

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Provider-agnostic LLM client: many providers over direct `httpx` (no vendor SDKs), with PII redaction, signing hooks, OTel, and audit.

## Layer

**Provider abstraction.** Depends on `arctrust`, `arcstore`. ArcRun consumes it; ArcLLM knows nothing about ArcRun, ArcAgent, gateways, or user interfaces.

**Concern boundary:** all LLM calls live here. No agent state, no ReAct loop.

## Layout

```
src/arcllm/
  registry.py      # load_model / provider registry (always returns a router) — stays in arcllm
  types.py         # Message, Tool, LLMResponse, Delta — shared contract
  config.py        # layered TOML: packaged defaults + ~/.arc overrides
  embeddings.py    # embed() + resolve_embedder (lazy; arcllm[local] backend)
  adapters/        # httpx-shaped provider adapters (17)
  providers/       # per-provider TOML catalogs (models, pricing, capabilities)
  modules/         # routing, retry, fallback, guardrails, injection, security, otel, …
  backends/        # vault/secret backends (e.g. aws_secrets)
  trace_store.py   # hash-chained JSONL capture; trace_query.py = replay
  vault.py
```

## Entry points

Eager: `load_model`, message/tool/response/stream types (`Message`, `LLMResponse`, `Delta`), config loaders, exceptions.  
Lazy (`__getattr__`): adapters, modules, embeddings (`embed`, `resolve_embedder`), trace store + replay (`load_for_replay`).

## Package rules

- **No vendor LLM SDKs** — httpx only (root CLAUDE.md / tech.md).
- Cross-package consumers use `import arcllm` and qualified root-facade names, never its internal module layout.
- ArcRun owns the higher-level model-execution seam; ArcLLM remains the standalone provider/router implementation beneath it.
- Export cross-package contracts (the `Delta` stream frame, `LLMResponse`, `Message`) through the `arcllm` root facade.
- `load_model` always returns a `RoutingModule` — one declared model is a zero-cost pass-through; never reintroduce an "adapter-or-router" fork.
- Package `.env` must not inject API keys into unrelated consumers; respect cwd/.env conventions.

## Tests

`packages/arcllm/tests/` (+ `tests/security/`) — providers, modules, PII, embeddings, spool, import isolation.

## Working here

New provider = adapter under `adapters/`/`providers/` matching existing httpx patterns. Streaming: implement the shared stream contract; keep an unbreakable single-event fallback on the base path.
