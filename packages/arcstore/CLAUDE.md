# arcstore

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Operational / observability data plane: always-on local spool plus pluggable backends for tasks, runs, approvals, and cancellations.

## Layer

**Data plane.** Depends only on `arctrust`. Direction: `arctrust ← arcstore ← {arcllm, arcrun, arcagent, arcteam, arcgateway, arcui, …}`. No upward imports (`tests/architecture/test_no_arcstore_arcteam_upward_imports.py`).

## Layout

```
src/arcstore/
  spool.py          # Always-on ambient spool — fail-open
  records.py
  config.py         # ArcStoreConfig, data-dir resolution
  tasks.py          # Task store / claim semantics
  runs.py           # Run store (SPEC-061 ArcFlow substrate)
  approvals.py
  cancellations.py
  ingest.py
  query.py
  backends/         # sqlite, …
```

## Entry points

Phase-1 spool: `SpoolRecord`, `record`, `read`, `spool_path`, `resolve_data_dir`, `ArcStoreConfig`. Domain APIs as `arcstore.tasks`, `arcstore.runs`, etc.

## Package rules

- Spool is **fail-open / ambient**; durable store backfills later — don't make spool require a healthy backend.
- Never import agent, UI, loop, or gateway packages.
- Shared `resolve_data_dir` — don't invent parallel path conventions.
- Task claim / board-move semantics matter (e.g. operators can't move into `in_progress` casually) — preserve existing invariants when editing `tasks.py`.

## Tests

`packages/arcstore/tests/unit/` + `integration/` — spool, tasks, runs, backends, claim/audit, import isolation.

## Working here

ArcFlow (SPEC-061) uses `tasks` + `runs` as execution substrate from `arcteam`. Additive store capabilities preferred over breaking the closed spool kind set without an explicit decision.
