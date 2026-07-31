# SPEC-026 — Arcstore Operational Storage: Implementation Plan

**Status:** COMPLETE
**Phases:** 5 (Spool seam → arctrust WORM → Store layer → Wire-in & UI cutover → CLI lifecycle & shared config)
**Approval gates:** end of each phase
**Pillar trace:** every task lists its primary pillar(s) — Simplicity (S), Modularity (M), Security (Sec), Scalability (Sc).
**Module discipline:** tasks do not cross module boundaries. A task touching two packages is split. TDD: write the failing test first (the `Test` column names it).

---

## Phase 1 — `arcstore.spool` seam (FR-2) — *start here*

The always-on recorder everything writes through. Proves the "call now, see later" guarantee before anything else exists.

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| 1.1 | Scaffold `packages/arcstore` (pyproject: name=arcstore, dep pydantic + arctrust, extras `[postgres]`,`[cloud]`); register in workspace; ruff/mypy config matching arctrust | `arcstore` | S, M | `pip install -e` resolves; `mypy --strict` + `ruff check` clean on empty package | [x] |
| 1.2 | Failing test: `SpoolRecord` flat model validates llm_call fields + auto ts | `arcstore` | S | `tests/unit/test_records.py::test_spool_record_llm_call_fields_and_auto_ts` | [x] |
| 1.3 | Implement `records.py::SpoolRecord` (frozen, flat per SDD §4.1) | `arcstore` | S | 1.2 passes | [x] |
| 1.4 | Failing test: `record()` appends one durable JSON line with only spool imported (no backend) | `arcstore` | S, M | `tests/unit/test_spool.py::test_record_appends_durable_line_without_store` | [x] |
| 1.5 | Implement `spool.py::record/read/spool_path` (stdlib+pydantic, append-only, fail-open) | `arcstore` | S | 1.4 passes | [x] |
| 1.6 | Failing test: a raised write error is swallowed + logged; caller proceeds (AU-5) | `arcstore` | Sec, Sc | `tests/unit/test_spool.py::test_record_is_fail_open_on_write_error` | [x] |
| 1.7 | Implement fail-open guard in `record()` | `arcstore` | Sec | 1.6 passes | [x] |
| 1.8 | Architecture test: importing `arcstore.spool` imports no DB/cloud driver (FR-2 AC-2.2) | `arcstore` | Sc | `tests/unit/test_import_isolation.py::test_spool_import_pulls_no_backend` | [x] |
| 1.9 | Perf test: spool write p95 < 5 ms; no event-loop block | `arcstore` | Sc | `tests/unit/test_spool_perf.py::test_record_under_5ms_p95` | [x] |
**Phase 1 acceptance:** AC-2.1, AC-2.2, AC-2.3, AC-2.4 pass.

---

## Phase 2 — arctrust durable WORM (FR-1)

Collapse the two sinks into one durable signed chain. Delete the in-memory split.

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| 2.1 | Failing test: `WormSink.write` persists N signed-chained lines; fresh instance over same file restores `chain_tip` (survives restart) | `arctrust` | Sec, S | `tests/test_audit.py::test_worm_persists_and_restores_chain_tip` | [x] |
| 2.2 | Implement `WormSink` (append signed line, restore tip from file tail on init, 0600 append-only) | `arctrust` | Sec | 2.1 passes | [x] |
| 2.3 | Failing test: mutating any persisted byte → `verify_chain()==False` | `arctrust` | Sec | `tests/test_audit.py::test_tampered_line_fails_verify` | [x] |
| 2.4 | Failing test: forged `event_hash` with bad signature → `verify_chain()==False` | `arctrust` | Sec | `tests/test_audit.py::test_forged_signature_fails_verify` | [x] |
| 2.5 | Implement `verify_chain()` reading file (links + Ed25519 signature) | `arctrust` | Sec | 2.3, 2.4 pass | [x] |
| 2.6 | Failing test: `emit()` to WORM is fail-open on IO error (AU-5) | `arctrust` | Sec, Sc | `tests/test_audit.py::test_emit_worm_fail_open` | [x] |
| 2.7 | **Delete** `JsonlSink` + `SignedChainSink`; update `__all__`; make `WormSink` the default sink; update all callers (no shim) | `arctrust` | S, M | repo-wide: no `JsonlSink`/`SignedChainSink` refs; full suite green | [x] |
| 2.8 | Architecture test: `arctrust` imports no Arc package (FR-1 AC-1.5) | `arctrust` | M | `tests/test_layering.py::test_arctrust_imports_no_arc_package` | [x] |

**Phase 2 acceptance:** AC-1.1–AC-1.5 pass. `ruff`/`mypy --strict` clean (fix any inherited errors touched — CLAUDE.md).

---

## Phase 3 — Store layer: Protocol + SqliteBackend + ingest (FR-3)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| 3.1 | Failing test: `StorageBackend` Protocol satisfied by a `FakeBackend` (in-memory) — upsert/query/begin | `arcstore` | M | `tests/unit/test_backend_protocol.py::test_fake_backend_conforms` | [x] |
| 3.2 | Implement `backends/base.py` Protocol + `backends/memory.py` FakeBackend | `arcstore` | M | 3.1 passes | [x] |
| 3.3 | Failing test: `SqliteBackend` upsert is idempotent (same record twice → one row) | `arcstore` | S, Sec | `tests/unit/test_sqlite_backend.py::test_upsert_idempotent` | [x] |
| 3.4 | Implement `backends/sqlite.py` (WAL, tables per SDD §5.2, identity keying) | `arcstore` | S | 3.3 passes | [x] |
| 3.5 | Failing test: records written to spool while store down appear after `backfill()` (UC-1) | `arcstore` | S, Sec | `tests/integration/test_ingest.py::test_backfill_recovers_offline_records` | [x] |
| 3.6 | Failing test: lines appended while running appear via `tail()`; offset persists across restart | `arcstore` | Sc | `tests/integration/test_ingest.py::test_tail_follows_and_resumes_from_offset` | [x] |
| 3.7 | Implement `ingest.py::StoreIngest` (backfill + tail, persisted byte offset, idempotent) | `arcstore` | S, Sc | 3.5, 3.6 pass | [x] |
| 3.8 | Failing test: WORM ingest verifies chain and flags unverified rows on tamper | `arcstore` | Sec | `tests/integration/test_ingest.py::test_worm_ingest_flags_tamper` | [x] |
| 3.9 | Implement WORM ingest via `arctrust` verify; `query.py` read API | `arcstore` | Sec, M | 3.8 passes | [x] |
| 3.10 | Architecture test: no SQLite type leaks into the Protocol (proven by FakeBackend swap) | `arcstore` | M | `tests/unit/test_backend_protocol.py::test_query_runs_on_fake_and_sqlite` | [x] |

**Phase 3 acceptance:** AC-3.1–AC-3.4 pass.

---

## Phase 4 — Wire-in & UI cutover (FR-4, FR-5)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| 4.1 | Failing test: a direct arcllm call records an `llm_call` spool line with token/cost/latency | `arcllm` | S, M | `tests/.../test_llm_spool.py::test_completion_records_llm_call` | [x] |
| 4.2 | Implement arcllm client hook (`finally`-guarded `record()`, `arcstore.enabled` gate) | `arcllm` | M | 4.1 passes | [x] |
| 4.3 | Failing test: arcllm call that raises records `outcome="error"` | `arcllm` | Sec | `tests/.../test_llm_spool.py::test_error_call_records_outcome_error` | [x] |
| 4.4 | Failing test: `arcstore.enabled=false` → no spool writes | `arcllm` | M | `tests/.../test_llm_spool.py::test_disabled_records_nothing` | [x] |
| 4.5 | Implement arcrun loop `run_event` hooks (start/step/finish) | `arcrun` | M | `tests/.../test_run_spool.py::test_loop_emits_run_events` | [x] |
| 4.6 | Architecture test: arcllm/arcrun import `arcstore.spool` only, not the store/backends | `arcllm`,`arcrun` | M, Sc | `tests/.../test_import_isolation.py::test_producers_import_spool_only` | [x] |
| 4.7 | **Delete** `arcui.bridge.UIBridgeSink` + bridge module + sink registration | `arcui` | S | grep test: no `UIBridgeSink` ref anywhere (AC-5.1) | [x] |
| 4.8 | Failing test: arcui history route returns data via `arcstore.query`; surviving server restart loses no history | `arcui` | S, Sc | `tests/test_routes.py` (Observe-backed) + `tests/integration/test_full_trace_flow.py` | [x] |
| 4.9 | Implement arcui routes to read arcstore query API (replace pushed-event consumption) | `arcui` | M | traces/stats/timeseries/performance/cost-efficiency/export/per-agent → `app.state.observe` | [x] |
| 4.10 | ~~Front-end: poll with visibility-gating~~ **SUPERSEDED** (user 2026-05-31): read-on-demand CRUD, NO polling | `arcui` | Sc | n/a — polling machinery intentionally not built | [x] |
| 4.11 | Architecture test: `emit()` has one default sink (WORM); arcui is not a sink/subscriber | `arctrust`,`arcui` | M | `arctrust tests/test_layering.py::test_emit_single_default_sink_no_ui_coupling` + `arcui tests/test_no_push_pipeline.py` (AC-5.4) | [x] |
| 4.12 | Full-suite + `mypy --strict` + `ruff` green across arcstore/arctrust/arcllm/arcrun/arcui; fix any inherited gate errors touched | (all) | — | CI green; quality gates pass | [x] |

**Phase 4 acceptance:** AC-4.1–AC-4.4, AC-5.1–AC-5.4 pass.

---

## Phase 5 — CLI lifecycle + shared TOML config (FR-6, FR-7)

Makes arcstore ambient infrastructure: agent lifecycle spins it up; one shared config schema drives arcllm/arcrun/arccli.

### Track A — Shared config (FR-7)
| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| 5.1 | Failing test: `ArcStoreConfig` validates `[arcstore]` block (enabled/data_dir/backend/store_raw_bodies/rotation/sample_rate) | `arcstore` | S | `tests/unit/test_config.py::test_arcstore_config_block` | [x] |
| 5.2 | Implement `arcstore.config.ArcStoreConfig` + `resolve_data_dir()` (env > toml > default precedence) | `arcstore` | S, M | 5.1 passes | [x] |
| 5.3 | Failing test: arcllm, arcrun, arccli resolve the **same** data_dir from identical config+env (AC-7.1) | `arcstore` | M | `tests/integration/test_shared_data_dir.py::test_all_entry_points_agree_on_data_dir` | [x] |
| 5.4 | arcllm + arcrun reference the **one** `arcstore.config` resolver (via `arcstore.spool`); `ArcStoreConfig` is single-source, no redefinition (AC-7.3) | `arcllm`,`arcrun` | M | `tests/integration/test_shared_data_dir.py::test_arcstore_config_is_single_source` + `test_producers_import_the_one_resolver` | [x] |
| 5.5 | Failing test: `enabled=false` disables spool+store for that entry point (AC-7.4) | `arcstore` | M | `tests/unit/test_config.py::test_disabled_short_circuits` | [x] |

### Track B — Lifecycle spin-up (FR-6)
| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| 5.6 | Failing test: `arc agent create` writes `[arcstore]` block + creates data dir/spool (AC-6.1) | `arccli` | S | `tests/test_agent_create.py::test_create_scaffolds_arcstore` | [x] |
| 5.7 | Implement create scaffolding + `arc store init` | `arccli` | S | 5.6 passes | [x] |
| 5.8 | Failing test: serve/run spin-up via `managed_store_ingest` starts `StoreIngest` and stops it cleanly, no orphan task (AC-6.2) | `arccli` | M, Sc | `tests/test_store_lifecycle.py::test_enabled_starts_and_stops_ingest_no_orphan` | [x] |
| 5.9 | Implement spin-up sequence per SDD §12.2 (always-spool, ingest-if-enabled, fail-open); wired into `arc agent serve`/`run` | `arccli` | Sec, Sc | 5.8 passes + serve/run wrapped in `managed_store_ingest` | [x] |
| 5.10 | Failing test: broken backend → agent still starts, spool still records (degraded) (AC-6.3) | `arccli` | Sec | `tests/test_store_lifecycle.py::test_broken_backend_is_fail_open` + `test_store_cli.py::test_status_reports_paths` | [x] |
| 5.11 | Implement `arc store` group (`init`/`up`/`status`/`verify`/`backfill`); `verify` non-zero on tamper (AC-6.4) | `arccli` | Sec | `tests/test_store_cli.py::test_verify_exit_code_nonzero_on_tamper` | [x] |

**Phase 5 acceptance:** AC-6.1–AC-6.4, AC-7.1–AC-7.4 pass.

---

## Research-Driven Additions (from /deepen, 2026-05-31)

These tasks were surfaced by parallel research (see SDD §11). They are additive to the phase tables above.

### Phase 1 additions (spool)
| # | Task | Module | Pillar | Test |
|---|---|---|---|---|
| 1.10 | Use `os.write(os.open(..., O_WRONLY\|O_APPEND\|O_CREAT, 0o600))` single-syscall append (not `file.write()`); no fsync, no lock | `arcstore` | S, Sc | `test_spool.py::test_record_single_syscall_0600` |
| 1.11 | Daily file rotation `operational-YYYY-MM-DD.jsonl` (clone `JSONLTraceStore`) | `arcstore` | S | `test_spool.py::test_daily_rotation` |

### Phase 2 additions (WORM) — closes defects C1/C4
| # | Task | Module | Pillar | Test |
|---|---|---|---|---|
| 2.9 | `verify_chain(public_key)` verifies **Ed25519 signature** per record (defect C1) | `arctrust` | Sec | `test_audit.py::test_verify_checks_signatures_not_just_links` |
| 2.10 | Monotonic `seq` in each record + hash; gap → verify fails (defect C4) | `arctrust` | Sec | `test_audit.py::test_seq_gap_truncation_detected` |
| 2.11 | `flock` single-writer; torn-line restart truncate + signed `recovery` record | `arctrust` | Sec | `test_audit.py::test_torn_line_recovery_and_single_writer` |
| 2.12 | Canonical `model_dump(mode="json")` (drop `default=str`); rotate at 100k/50MB; stream-verify (no all-records-in-RAM) | `arctrust` | S, Sc | `test_audit.py::test_canonical_serialization_and_streaming_verify` |
| 2.13 | Out-of-band genesis tip assert via `trust_store` on startup | `arctrust` | Sec | `test_audit.py::test_genesis_tip_anchored` |

### Phase 3 additions (store) — closes defect C5
| # | Task | Module | Pillar | Test |
|---|---|---|---|---|
| 3.11 | Match `arcteam.storage` house style: **async** `StorageBackend` Protocol, `asyncio.to_thread` sqlite bridge; **drop `begin()` from Protocol** (txn internal to backend) | `arcstore` | M, S | `test_backend_protocol.py::test_async_protocol_no_begin` |
| 3.12 | PRAGMA stack: WAL, `synchronous=NORMAL`, **`busy_timeout`** (defect C5), `journal_size_limit=64MB`, `BEGIN IMMEDIATE` writes, batch 100–500/txn | `arcstore` | Sc | `test_sqlite_backend.py::test_pragmas_and_batched_commit` |
| 3.13 | Per-instance DB file enforced (shared-nothing, NFR-8); content-derived idempotency key (never offset/rowid) | `arcstore` | M, Sc | `test_sqlite_backend.py::test_per_instance_file_and_content_key` |

### Phase 4 additions (UI) — closes defects C2/C3
| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| 4.13 | Flip `store_raw_bodies` default → `False` + startup warning; update test (defect C2) | `arcllm` | Sec | `test_telemetry.py` | [x] |
| 4.14 | ~~Cursor-incremental UI fetch~~ **SUPERSEDED** by read-on-demand (no polling) — full-window read per request is cheap at single-operator scale | `arcui` | S, Sc | n/a | [x] |
| 4.15 | Delete dead push machinery: `useLiveStore`, `arcSocket`/`ws.ts`, `connection` store, `EventBuffer`, `SubscriptionManager`, `ConnectionManager`, `RollingAggregator`, `reporter`, `bridge`, `file_change_bridge`, `team_chat_bridge`, `/ws`+`/ws/dashboard`+`/api/agent/connect` routes, gateway `dashboard_bus`, arcagent `ui_reporter`; connection status → React Query | `arcui`,`arcgateway`,`arcagent` | S, M | `tests/test_no_push_pipeline.py` — none remain | [x] |
| 4.16 | (Deferred-ready) document `PRAGMA data_version`→SSE signal path; do NOT build `update_hook` | `arcui` | M | doc note in SDD §11.6 | [x] |

---

## Definition of Done (whole spec)

- [x] All phase acceptance criteria pass (fresh test output).
- [x] Import DAG enforced by test (SDD §2); arctrust imports no Arc package.
- [x] No `UIBridgeSink`, `JsonlSink`, or `SignedChainSink` references remain.
- [x] UC-1 (call-now-see-later) demonstrated end-to-end (`test_store_cli.py::test_backfill_then_query_roundtrip` + ingest integration tests).
- [x] `ruff check`, `mypy --strict` green on all changed src; suites green per-package (arcstore 56, arccli 318, arcllm 925, arcrun 407, arctrust 182).
- [x] README status → COMPLETE.

## Suggested Branch

```bash
git checkout -b feature/SPEC-026-arcstore-operational-storage
git add .claude/specs/SPEC-026-arcstore-operational-storage/ docs/architecture/decisions/ADR-022-*.md
git commit -m "spec(SPEC-026): arcstore operational storage + arctrust durable WORM"
```
