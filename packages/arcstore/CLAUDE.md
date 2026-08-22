# arcstore

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Operational / observability data plane: an always-on local spool plus the PostgreSQL backend for tasks, runs, approvals, cancellations, and queries. Supabase is supported through its PostgreSQL URL.

## Layer

**Data plane.** Depends only on `arctrust`. Direction: `arctrust ← arcstore ← {arcllm, arcrun, arcagent, arcteam, arcgateway, arcui, …}`. No upward imports (`tests/architecture/test_no_arcstore_arcteam_upward_imports.py`).

## Layout

```
src/arcstore/
  spool.py          # Always-on ambient spool — fail-open
  records.py        # SpoolRecord (metadata-only by default)
  config.py         # ArcStoreConfig, PostgreSQL URL/pool settings, resolve_data_dir
  tasks.py          # Task model + TaskStore (atomic claim / board moves)
  runs.py           # Run model + RunStore (SPEC-061 ArcFlow substrate)
  approvals.py      # Mechanical operator-signed approval spine (SPEC-035)
  cancellations.py  # Operator kill-switch / stale-cancel age-out
  ingest.py         # StoreIngest — crash-safe spool + WORM tailer
  query.py          # Read API over the ingested backend
  backends/         # ArcStoreBackend: PostgreSQL production backend + in-memory test fake
```

## Entry points

Package root: `SpoolRecord`, `record`, `read`, `spool_path`, `resolve_data_dir`, `ArcStoreConfig`. Domain APIs under their submodules — `arcstore.tasks` (`Task`, `TaskStore`), `arcstore.runs` (`Run`, `RunStore`), `arcstore.ingest`, `arcstore.query`, `arcstore.backends`.

## Package rules

- Spool is **fail-open / ambient**; durable store backfills later — don't make spool require a healthy backend.
- ArcStore's durable query/mutation plane is PostgreSQL. Resolve the DSN from `ARCSTORE_DATABASE_URL` or the configured vault credential reference; never add a SQLite production fallback.
- Supabase direct connections use port 5432; transaction poolers use port 6543 and disable prepared-statement caching. External connections require TLS.
- Never import agent, UI, loop, or gateway packages.
- Shared `resolve_data_dir` — don't invent parallel path conventions.
- Task claim / board-move semantics matter (e.g. operators can't move into `in_progress` casually) — preserve existing invariants when editing `tasks.py`.

## Tests

`packages/arcstore/tests/unit/` + `integration/` — spool, tasks, runs, backends, claim/audit, import isolation.

## Working here

ArcFlow (SPEC-061) uses `tasks` + `runs` as execution substrate from `arcteam`. Additive store capabilities preferred over breaking the closed spool kind set without an explicit decision.
