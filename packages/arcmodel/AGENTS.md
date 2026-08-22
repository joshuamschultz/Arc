# arcmodel

> **Build standards:** repo root [`AGENTS.md`](../../AGENTS.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Reserved home for cross-tenant model selection, capability discovery, and tiered routing. **Currently scaffolding** — not a live public API.

## Layer

Planned to sit beside `arcllm` (routing lifted out of provider configs). No Arc deps declared yet. Nothing imports it.

## Layout

```
src/arcmodel/
  __init__.py     # __version__ only
```

## Entry points

None yet.

## Package rules

- Status = early scaffolding. **Do not invent a public API** or dump ad-hoc routing here without an explicit product/design decision.
- Real provider HTTP stays in `arcllm` until this package has a defined seam.
- Prefer extending `arcllm` registry/config for near-term needs unless a spec owns this package.

## Tests

None yet.

## Working here

If implementing routing: start from a spec (PRD/SDD), define the contract with `arcllm`, then grow this package deliberately. Empty package ≠ invitation to park unrelated code.