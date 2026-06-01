# SPEC-028 — ArcRun Tool / Code / Spawn Observability: Implementation Plan

**Status:** PENDING (DEEPENED 2026-05-31 — see SDD §11)
**Phases:** 4 (spool kinds → arcrun tool spooling → spawn identity+lineage → arcui surfaces)
**Approval gates:** end of each phase
**Pillar trace:** every task lists its primary pillar(s) — Simplicity (S), Modularity (M), Security (Sec), Scalability (Sc).
**Module discipline:** arcrun records its own tool events; arcagent owns spawn; arcstore owns kinds; arcui only reads. A task touching two packages is split. TDD: failing test first (the `Test` column names it).

> **/deepen corrections (SDD §11.0) — fold these in before building:**
> - **C1:** `tool.end` emits only `result_length`, not the body. Compute `args/result` digest+size in `arcrun/executor.py` (where the data lives), not in `events.py`. See tasks 2.4a/2.5.
> - **C2:** child `llm_call` identity must use **contextvars**, not `set_global_defaults` (global race under `spawn_many`). See tasks 3.2a/3.4.
> - Field names align to OTel GenAI semconv (`gen_ai.tool.name`, `…operation.name=execute_tool`, `error.type`); flat record stays flat (not OTLP-file format).
> - `[arcstore].sample_rate` is currently **unwired** — wire it at EventBus level, tool_event only (never errors/run_event/spawn_event). See task 2.6.

---

## Phase 1 — arcstore: tool_event + spawn_event kinds (FR-1, FR-3 data model)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| 1.1 | Failing test: `SpoolRecord(kind="tool_event", …)` validates new flat fields (tool_name/phase/args_digest/result_digest/sizes) + auto ts | `arcstore` | S | `tests/unit/test_records.py::test_tool_event_fields` | [ ] |
| 1.2 | Failing test: `SpoolRecord(kind="spawn_event", …)` validates parent_did/child_did/role/depth | `arcstore` | S | `tests/unit/test_records.py::test_spawn_event_fields` | [ ] |
| 1.3 | Add `tool_event`,`spawn_event` to `SpoolKind` + fields to `SpoolRecord` | `arcstore` | S | 1.1, 1.2 pass | [ ] |
| 1.4 | Failing test: `table_for_kind` maps the two new kinds; `OPERATIONAL_TABLES` includes them | `arcstore` | M | `tests/unit/test_backend_protocol.py::test_new_kinds_mapped` | [ ] |
| 1.5 | Add tables to `_KIND_TABLE`/`OPERATIONAL_TABLES` + `SqliteBackend` schema (flat cols, INSERT OR IGNORE) | `arcstore` | S | 1.4 passes | [ ] |
| 1.6 | Failing test: ingest round-trips a `tool_event` + `spawn_event` from spool → query (idempotent on replay) | `arcstore` | S, Sec | `tests/integration/test_ingest.py::test_tool_and_spawn_ingest` | [ ] |
| 1.7 | Verify FakeBackend still conforms (no schema change needed) | `arcstore` | M | existing `test_backend_protocol` green | [ ] |

**Phase 1 acceptance:** new kinds validate, map, ingest idempotently; ruff + mypy --strict clean.

---

## Phase 2 — arcrun: spool tool events (FR-1, FR-2)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| 2.1 | Failing test: a run with a tool call spools `tool_event` start + end (name/outcome/latency) | `arcrun` | S | `tests/test_tool_spool.py::test_tool_events_spooled` | [ ] |
| 2.2 | Failing test: `store_raw_bodies=false` → digests/sizes only, no args/result body | `arcrun` | Sec | `tests/test_tool_spool.py::test_tool_event_metadata_only_default` | [ ] |
| 2.3 | Failing test: a raising tool spools `phase="error"`, `outcome="error"` | `arcrun` | Sec | `tests/test_tool_spool.py::test_tool_error_spooled` | [ ] |
| 2.4a | **(C1)** Implement digest-at-source in `executor.py`: compute `args_digest`/`args_size` on `tool.start` and `result_digest`/`result_size` on `tool.end` (where `tc.arguments`/`result` exist), emit on the events; bodies added only when `store_raw_bodies` | `arcrun` | Sec, S | `tests/test_tool_spool.py::test_result_digest_is_content_not_length` | [ ] |
| 2.4 | Implement `EventBus` tool→spool mapping in `_record_run_event` (consume the executor-computed digest/size fields; `store_raw_bodies` flag on EventBus ctor + `run()`/`run_async()`) | `arcrun` | S, Sec | 2.1–2.4a pass | [ ] |
| 2.5 | Failing test: code-exec tool produces a `tool_event` identifiable as code-exec with `code_digest`/`code_size` always present; body only under flag | `arcrun` | S, Sec | `tests/test_tool_spool.py::test_code_exec_event` | [ ] |
| 2.6 | Failing test + impl: wire `sample_rate` (currently UNWIRED) at EventBus level via caller-passed rate; `<1` thins `tool_event` but never `run_event`/`spawn_event`/errors | `arcrun` | Sc, Sec | `tests/test_tool_spool.py::test_tool_events_sampled_lifecycle_and_errors_kept` | [ ] |
| 2.7 | Architecture test: arcrun still imports only `arcstore.spool` (no backend, no config) | `arcrun` | M, Sc | `tests/test_import_isolation.py::test_arcrun_spool_only` | [ ] |
| 2.8 | `tool_event` field names align to OTel GenAI semconv (`gen_ai.tool.name` etc.); record stays flat (doc note in SDD §11.1) | `arcrun`,`arcstore` | M | doc-only; assert column names in `test_records.py` | [ ] |

**Phase 2 acceptance:** AC-1.1–1.4, AC-2.1–2.2 pass; gates clean.

---

## Phase 3 — arcagent: spawn identity + lineage (FR-3)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| 3.1 | Failing test: spawned child's `run_event`s spool under the child `actor_did` (not parent) — fix is `actor_did=child_did` at BOTH call sites (`spawn.py:156`, `spawn.py:495`) | `arcagent` | M | `tests/.../test_spawn_observability.py::test_child_run_events_tagged` | [ ] |
| 3.2a | **(C2)** Implement contextvar identity in `arcllm.telemetry`: read `agent_did`/`agent_label` from a `ContextVar` (fallback config>global); concurrency-safe, no `set_global_defaults` | `arcllm` | M, Sc | `tests/test_telemetry.py::test_agent_identity_from_contextvar` | [ ] |
| 3.2b | Failing test: two CONCURRENT children (`spawn_many`) get correct distinct identities — no cross-contamination | `arcagent` | Sc, Sec | `tests/.../test_spawn_observability.py::test_concurrent_children_not_cross_attributed` | [ ] |
| 3.2 | Failing test: spawned child's `llm_call`s carry child `agent_did`/`agent_label`, distinct from parent (spawn sets the contextvar around child `run()`, capture+try/finally reset) | `arcagent` | M | `tests/.../test_spawn_observability.py::test_child_llm_calls_separated` | [ ] |
| 3.3 | Failing test: a `spawn_event` records parent→child edge (parent_did/child_did/role/depth); spawn.py imports `arcstore.spool` | `arcagent` | Sec | `tests/.../test_spawn_observability.py::test_spawn_lineage_recorded` | [ ] |
| 3.4 | Implement: `actor_did=child_did` into child `run()`; set telemetry contextvar around the run; emit `spawn_event`; keep `_emit_spawn_audit` (arctrust) | `arcagent` | M, Sec | 3.1–3.3 pass | [ ] |
| 3.5 | Failing test: child re-identification failure degrades (run_events still tagged) not breaks | `arcagent` | Sec | `tests/.../test_spawn_observability.py::test_child_identity_degrades_safely` | [ ] |
| 3.6 | Architecture test: arcrun source unchanged by spawn work; spawn stays arcagent; telemetry identity stays arcllm | `arcagent`,`arcrun` | M | `tests/.../test_layering.py::test_spawn_owned_by_arcagent` | [ ] |

**Phase 3 acceptance:** AC-3.1–3.4 pass; gates clean.

---

## Phase 4 — arcui: surfaces (FR-4)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| 4.0 | Ensure `tool_event` carries `request_id == run_id` so a run's llm_call + run_event + tool_event streams join on one key (research §11.4) | `arcrun`,`arcstore` | S | covered by 2.4 + `test_observe.py::test_timeline_joins_on_run_id` | [ ] |
| 4.1 | Failing test: `Observe.tool_events(run_id)` returns ordered tool/code events (query-per-table, merge by `ts` in Python) | `arcui` | S | `tests/test_observe.py::test_tool_events_query` | [ ] |
| 4.2 | Failing test: `Observe.spawn_tree(...)` assembles parent→child tree from `spawn_events` | `arcui` | S | `tests/test_observe.py::test_spawn_tree_query` | [ ] |
| 4.3 | Failing test: `Observe.llm_by_identity(window)` separates parent vs child by `agent_label` | `arcui` | M | `tests/test_observe_stats.py::test_llm_by_identity` | [ ] |
| 4.4 | Implement Observe queries | `arcui` | S, M | 4.1–4.3 pass | [ ] |
| 4.5 | Failing test: read routes return tool timeline + spawn tree + identity cost JSON | `arcui` | M | `tests/test_routes.py::test_tool_and_lineage_routes` | [ ] |
| 4.6 | Implement read routes (pull, short-lived query, `agent_label` filter) | `arcui` | M, Sc | 4.5 passes | [ ] |
| 4.7 | Frontend: per-run tool/code timeline, spawn lineage tree, identity cost breakdown (read-on-demand React Query, no polling) | `arcui` | S, Sc | `tests/unit/test_react_frontend.py` (route/asset wiring) | [ ] |
| 4.8 | Integration: run-with-code + spawn → arcui shows code, child I/O, separated cost; server restart loses nothing | `arcui` | Sc | `tests/integration/test_tool_spawn_flow.py` | [ ] |
| 4.9 | Architecture test: arcui reads only (not a sink/subscriber); no push reintroduced | `arcui` | M | existing `test_no_push_pipeline.py` extended | [ ] |

**Phase 4 acceptance:** AC-4.1–4.4 pass; gates clean.

---

## Definition of Done (whole spec)

- [ ] All phase acceptance criteria pass (fresh test output).
- [ ] **C1 closed:** `result_digest` is a digest of the result *content*, not its length (digest computed at source in `executor.py`).
- [ ] **C2 closed:** concurrent `spawn_many` children are never cross-attributed (contextvar identity test green).
- [ ] Import DAG holds: arcrun imports only `arcstore.spool`; spawn stays arcagent; telemetry identity stays arcllm; arcui is read-only.
- [ ] Metadata-only by default proven (no code/args/result/child bodies unless `store_raw_bodies=true`); errors + `run_event` + `spawn_event` never sampled out.
- [ ] UC-1 (see code), UC-2 (see spawned agent), UC-3 (separated cost) demonstrated end-to-end.
- [ ] `ruff check`, `mypy --strict`, coverage ≥80% green on all changed packages.
- [ ] README status → COMPLETE.

## Suggested Branch

```bash
git checkout -b feature/SPEC-028-arcrun-tool-observability
```
