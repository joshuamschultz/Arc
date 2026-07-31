---
spec_id: SPEC-026
name: arcstore-operational-storage
status: complete
created: 2026-05-31
type: db
intake_confidence: 0.93
type_confidence: 0.80
fast_track: true
prior_work:
  - docs/architecture/decisions/ADR-022-storage-split-arctrust-worm-arcstore-operational.md (authoritative design)
  - docs/architecture/decisions/ADR-019-four-pillars-universal.md (audit pillar — WORM stays in arctrust)
  - docs/architecture/decisions/ADR-020-arcgateway-as-data-plane.md (transport vs persistence boundary)
  - packages/arctrust/src/arctrust/audit.py (current JsonlSink / SignedChainSink to be collapsed)
related_specs:
  - SPEC-015-arcui-llm-telemetry (the telemetry arcui shows today — re-homed onto arcstore here)
  - SPEC-022-arcui-agents-live (live agent surface — switched from push sink to store-read here)
  - SPEC-019-arcui-zero-config (zero-config posture — arcstore must stay ambient/on-by-default)
trigger: arcui auto-tap of running agent histories misses and fails to update. Root cause — a live push sink (UIBridgeSink) runs as a parallel channel to the durable record and drops events; there is no always-on durable operational store to reconcile against. Research into builderz-labs/mission-control confirmed the fix — a canonical durable store read on reconciliation, live push only as optimization.
pillars_priority: [Simplicity, Modularity, Security, Scalability]
steering_status: present (.claude/steering/) — this spec conforms to tech.md package layout and structure.md module boundaries
---

# SPEC-026 — Arcstore Operational Storage

## TL;DR

Give Arc a durable, always-on **operational/observability data plane** (`arcstore`) and a real durable **WORM** in `arctrust`, so that every arcllm/arcrun/arcagent action is recorded the moment it happens — independent of whether any server, DB, or UI is running — and the UI reads that durable record instead of listening to a fragile live push.

Three threads:

1. **arctrust durable WORM** — collapse today's `JsonlSink` (durable, unchained) and `SignedChainSink` (chained+signed, **in-memory only**) into one durable, append-only, Ed25519-signed hash chain. Always on, every tier. The compliance system of record. arctrust still imports no Arc package.
2. **arcstore as ambient infrastructure** — a new package with two layers: `arcstore.spool` (always-on, server-independent, dependency-light append-only local recorder) and a `StorageBackend` query layer (default `SqliteBackend`) that **backfills from the spool and the WORM on startup**, then tails them. Auto-instruments arcllm and arcrun by default. "Call a setup arcllm from the CLI now, spin up arcstore later, see the previous calls."
3. **Kill the live push** — delete `UIBridgeSink` and its bridge. The emit fan-out collapses to a single WORM sink. arcui/arctui become **read-only** consumers of the store (poll, pause-on-inactive). The miss/no-update bug becomes structurally impossible — there is no parallel push channel left to drop.

**No new transport.** This is persistence, not a wire. arcgateway still owns the data plane (ADR-020).

## Pillar Trace (gating principle for every requirement)

| Pillar | What this spec delivers |
|---|---|
| **Simplicity** | One write path per concern (append to a durable file), one read path (UI ← SQLite), zero live wires between. `arcstore` is a pure file-tailer. The spool record is a flat JSONL line. Removing `UIBridgeSink` deletes code. |
| **Modularity** | Hard boundary: arctrust owns the compliance WORM, arcstore owns operational storage. One-directional import (`arctrust ← arcstore ← {arcllm, arcrun, arcagent, arcui}`). arcllm/arcrun keep their concern; recording is a default-on side-channel, not logic bleed. `StorageBackend` Protocol isolates the backend. |
| **Security** | WORM stays in the arctrust nucleus (preserves ADR-019 imports-nothing purity); durable signed chain satisfies AU-9/AU-10/AU-11. Spool write is fail-open (AU-5) — auditing/telemetry never breaks the audited call. Always-on at every tier (ADR-019 audit pillar). Cloud WORM-at-rest (SC-28) is an additive backend for federal. |
| **Scalability** | Spool is a non-blocking append; cold start stays <500ms because no DB driver loads on the spool path. SQLite (WAL) handles concurrent reads. Backend Protocol lets the same code scale to Postgres/cloud with DB-native change-notify replacing polling at scale. Stateless producers; the durable file is the coordination point. |

## Decisions Log

| ID | Decision | Rationale | Source |
|---|---|---|---|
| D-001 | `arcstore` is a **new package**; storage is not folded into arctrust | "Don't mix concerns" — persistence ≠ trust. Folding it in would force arctrust to import DB drivers, breaking ADR-019 nucleus purity | ADR-022, this conversation |
| D-002 | **WORM stays in arctrust**, not arcstore | arctrust imports no Arc package; the compliance record must live where the keypair/signing live. Keeps chain+signature together (no split) | ADR-022; ADR-019 §59 |
| D-003 | System of record = append-only **signed JSONL chain**; SQLite is a queryable **mirror** | Backend-portable compliance guarantee lives in the file, not the DB. SQLite-dev and cloud-WORM-prod produce the same verifiable record | ADR-022 |
| D-004 | **Local WORM + spool always on, every tier** (personal/enterprise/federal) | ADR-019: audit pillar is universal, not federal-gated. Observability is ambient, not opt-in | ADR-019; this conversation |
| D-005 | arcstore is **two layers**: server-independent `spool` (always-on) + DB query layer that backfills on startup | Enables "call now, spin up store later, see history." Spool has zero runtime-server dependency | This conversation |
| D-006 | arcstore is a **pure file-tailer** — it ingests the spool and the WORM files; it is **not** a sink in `emit()` | Single source of truth; no parallel emission path to diverge | This conversation |
| D-007 | **Delete `UIBridgeSink`** and the bridge; emit fan-out collapses to one WORM sink; arcui becomes read-only | The parallel push channel is the entire miss/no-update bug class. Per no-backward-compat directive, deleted not shimmed | This conversation; supersedes ADR-019 §Decision 4 |
| D-008 | Auto-instrument arcllm client + arcrun loop to spool **by default**, config-toggleable off | "Be part of the infrastructure" = on unless deliberately disabled | This conversation |
| D-009 | Default backend `SqliteBackend`; **Postgres + cloud WORM deferred** behind the `StorageBackend` Protocol until a concrete target exists | YAGNI — prove the seam with one backend (Simplicity > speculative Scalability) | ADR-022; CLAUDE.md |
| D-010 | arcllm depending on `arcstore.spool` is accepted (vs a zero-import filesystem contract) | "Part of the infrastructure" implies a shared recorder; spool is server-independent + dependency-light, so installed ≠ running | This conversation |
| D-011 | DB/cloud drivers are **optional extras** (`arcstore[postgres]`, `arcstore[cloud]`) never pulled by the spool path | Protects cold-start and baseline-memory budgets | ADR-022; CLAUDE.md scalability budgets |
| D-012 | Agent lifecycle (`arc agent create/serve/run`, `arc team`) **spins up arcstore ambiently**, fail-open; plus an explicit `arc store` group | Observability is infrastructure, not a manual step; store failure must never block the agent | User directive 2026-05-31 |
| D-013 | **One** `arcstore.config.ArcStoreConfig` + one `resolve_data_dir()`, referenced (not redefined) by arcllm/arcrun/arccli, with identical env>toml>default precedence | A direct `arc llm` call and a later `arc agent serve` must agree on the spool/store path or history fragments silently | User directive 2026-05-31 |

## References

- **ADR-022** — authoritative design (`docs/architecture/decisions/ADR-022-storage-split-arctrust-worm-arcstore-operational.md`)
- `packages/arctrust/src/arctrust/audit.py` — `AuditEvent`, `JsonlSink`, `SignedChainSink`, `emit()` (current state)
- `packages/arcui/` — `UIBridgeSink` + bridge to be removed; routes to switch to store-read
- builderz-labs/mission-control — framework-agnostic capture research (canonical durable store + reconciliation + live-push-as-optimization)

## Status Log

| Date | Status | Note |
|---|---|---|
| 2026-05-31 | PENDING | Spec generated from ADR-022. Awaiting approval to implement. |
| 2026-05-31 | DEEPENED | `/deepen` — 6 parallel research agents (web+codebase). Added SDD §11 Research Insights, ACs 1.6-1.8 / 4.5, NFR-7/8, 16 research-driven PLAN tasks. Surfaced 5 defects in existing code (C1-C5) + clone-from precedents (`JSONLTraceStore`, `SessionIndex`, `arcteam.storage`). |
| 2026-05-31 | SCOPED | Added FR-6 (CLI lifecycle spins up arcstore, fail-open + `arc store` group) and FR-7 (one shared `ArcStoreConfig` + `resolve_data_dir()` used by arcllm/arcrun/arccli). SDD §12-13, PLAN Phase 5, decisions D-012/D-013. Ready for implementation. |
| 2026-05-31 | PHASE 1 ✅ | `/implement` — `packages/arcstore` scaffolded + registered in uv workspace. Built `arcstore.spool` (records.py/spool.py/config.py): always-on, fail-open, `os.write` 0600 single-syscall, daily rotation, corrupt-line-skipping read, content-derived `record_id`. Tasks 1.1-1.11. **14 tests pass, ruff clean, mypy --strict clean, coverage 92%.** The call-now-see-later seam is proven. |
| 2026-05-31 | PHASE 3 ✅ | `/implement` — Store layer in `arcstore`: async `StorageBackend` Protocol (no `begin()`, research §11.3), `FakeBackend` (in-memory) + `SqliteBackend` (WAL + `synchronous=NORMAL` + `busy_timeout` [C5] + `journal_size_limit` + `BEGIN IMMEDIATE`, per-instance file [NFR-8], `executemany` batching, `INSERT OR IGNORE` content-keyed idempotency). `StoreIngest` pure file-tailer: backfill + tail with persisted per-file byte cursor (resumes, no re-scan), WORM verified on ingest via `arctrust.verify_chain` with per-row `verified` flag. `spool.read_from_offset` (torn-tail-safe) + `query.py` read API. Tasks 3.1-3.13. **39 tests pass (Protocol conformance proven on fake+sqlite), ruff clean, mypy --strict clean, coverage 94%.** |
| 2026-05-31 | PHASE 2 ✅ | `/implement` — Collapsed `JsonlSink` + `SignedChainSink` into one durable `WormSink` in `arctrust.audit`: append-only 0600 signed hash chain, tip restored from file tail (restart-safe), monotonic `seq` in the hash, lock-free module `verify_chain(path, pubkey)` checking links + **Ed25519 signatures (C1)** + seq gaps (C4) + genesis anchor, `flock` single-writer + torn-tail truncate→signed recovery record, canonical `model_dump(mode="json")` (C-2.12, no `default=str`), segment rotation + streaming verify. Old sinks deleted (no shim); README/docstrings updated; layering test added. Tasks 2.1-2.13. **181 arctrust tests pass, ruff clean, mypy --strict clean; arcgateway audit/pairing 110 pass.** |
| 2026-05-31 | PHASE 5 ✅ | `/implement` — **CLI lifecycle + shared config (FR-6/FR-7).** `arcstore.config.ArcStoreConfig` is now the one canonical `[arcstore]` schema (enabled/data_dir/backend/store_raw_bodies/rotation/retention/sample_rate, `sample_rate` bounded 0–1, `resolve_data_dir()` env>toml>default); arcllm + arcrun reach the spool through that **same** resolver (single-source, proven by `test_shared_data_dir.py`). New `arc store` group (`init`/`status`/`verify`/`backfill`/`up`) — `verify` exits non-zero on a tampered WORM chain, `backfill` re-ingests spool+WORM idempotently (UC-1 roundtrip). New `arccli.commands.agent._store_lifecycle.managed_store_ingest` spins arcstore up ambiently around `arc agent serve`/`run`: spool dir always created, `StoreIngest` backfill→tail started when enabled and stopped cleanly (no orphan task), **fail-open** on a broken backend (agent still starts, degraded). `arc agent create` now scaffolds an `[arcstore]` block + pre-creates the data dir/spool (AC-6.1). Added an autouse arccli conftest isolating `ARCSTORE_DATA_DIR` to a temp dir so the suite never pollutes `~/.arc/store`. Tasks 5.1–5.11. **Green (per-package, verified): arcstore 56, arccli 318, arcllm 925 (+1 skip), arcrun 407 (+4 skip), arctrust 182; ruff + mypy --strict clean on all changed src.** Spec status → COMPLETE. |
| 2026-05-31 | PHASE 4 ✅ | `/implement` — **Observe plane end-to-end + full push teardown.** arcui reads ONLY from arcstore through the `StorageBackend` Protocol via a new `arcstore.open_backend(name, path)` factory (UI is backend-agnostic; Postgres/cloud = config change, not code). New `arcui.observe.Observe` + `observe_stats` (SQL-style rollups for stats/timeseries/performance/cost-efficiency). Repointed routes: traces, stats, timeseries, performance, cost-efficiency, export, per-agent stats/traces. **Deleted the entire push pipeline:** `UIBridgeSink`/bridge, `EventBuffer`, `SubscriptionManager`, `ConnectionManager`, `RollingAggregator`, `reporter`, `file_change_bridge`, `team_chat_bridge`, `transport`/`transport_ws`, `federated_store`, routes `/ws` + `/ws/dashboard` + `/api/agent/connect` + `/api/schedule-history`, arcgateway `dashboard_bus`/`DashboardEventBus`, and the arcagent `ui_reporter` module (+ its tool_registry/module_bus/spawn hooks). Frontend rebuilt: removed `useLiveStore`/`arcSocket`/`ws.ts`/connection store → read-on-demand React Query (NO polling, per user — supersedes spec 4.10/4.14). Arch tests added (AC-5.1/5.4 + no-push-pipeline). Tasks 4.1-4.16. **Green: arcstore 43, arctrust 182, arcui 446, arcllm 925, arcrun 407, arccli 300, arcagent 3284, arcgateway 729; ruff + mypy --strict clean on all changed src.** (Recovered intact after a sub-agent `git reset --hard` wiped the uncommitted tree — restored from the dangling stash commit it left.) |
