# Changelog

All notable changes to ArcStore will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Durable inbox messages now retain typed attachment references for ArcTeam mail projections.

## [0.4.0] - 2026-08-22

- Production storage is PostgreSQL-only, using one async adapter for local
  PostgreSQL and Supabase with pooling, TLS and forward migrations.
- Complete mutable/CAS/batch/increment contract, fenced workflow writes, durable
  inbox, approval outbox leases and atomic transition-plus-notification methods.
- SQLite is removed from runtime; a resumable verified migration utility lives
  separately under `tools/one_time/` for later deletion.

### Changed
- Documentation refresh — README now covers the `runs` / ArcFlow surface, `create_batch`,
  the `mutable_increment` / `mutable_create_batch` primitives, and `store_db_path`.

## [0.3.0] - 2026-08-19

SPEC-056 Mission Control task-lifecycle hardening + the SPEC-061 ArcFlow execution substrate.
All new `Task` fields have defaults, so rows written before they existed still load.

### Added
- **Lifecycle timing + retry fields on `Task`** — `started_at`, `completed_at`,
  `duration_seconds` (the board's DONE-TODAY / AVG-TIME read these durable fields, not inferred
  timestamps); `attempts`, `max_attempts`, `last_error`, `next_attempt_at` (retry engine state);
  `timeout_seconds` + `cancel_requested` (per-task wall-clock cap + operator stop signal);
  `requires_review` (opt-in human gate); `classification` (no-write-down bound carried onto
  downstream notifications).
- **Deterministic run linkage** — `start_task` stamps `run_id` in the *same* atomic write that
  claims a task, so an `in_progress` task links its run from the moment it starts.
- **Race-safe terminal + retry transitions on `TaskStore`** — `finish`, `requeue`, `dead_letter`,
  `request_cancel`, `route` (assign an unowned task), `approve_review`/`reject_review` (operator
  resolves a `review` task), `set_status`, and `edit` (status-conditional at-rest edit, refuses
  `in_progress`). Each is a status-conditional `update_if`, so two contending actors resolve to
  exactly one winner.
- **Decomposition DAG support** — `children` (subtasks by `parent_id`), `deps_met`
  (all `blocked_by` done), `unassigned`, and `deps_would_cycle` (reject an edge that would form a
  cycle before it is ever written).
- **`delete`** — hard-delete a task row (operator action, attributed + audited).
- **`runs` domain (`arcstore.runs`, SPEC-061 ArcFlow COMP-006)** — a durable `Run` aggregate on
  a new `"runs"` collection of the shared mutable plane (the closed `SpoolKind` set is left
  untouched — no `run_id` added to `SpoolRecord`). `Run` / `PathEntry` / `RunBudget` mirror
  `tasks.py`'s conventions (frozen pydantic, one sanitized free-text field, optional audit sink).
  `RunStore` provides `create` / `get` / `list`, a status-conditional `transition`, a CAS-based
  `append_path_entry` (a plain merge-patch would replace the whole `path_taken` array instead of
  appending), and `reserve_budget` / `settle_budget` accounting.
- **`TaskStore.create_batch` (COMP-007)** — atomic cross-owner batch task creation for frontier
  materialization, idempotent on each task's own id so a crashed runner's replay returns the
  existing rows instead of duplicating them.
- **Two new mutable-plane backend primitives** — `mutable_create_batch` (`INSERT OR IGNORE`
  across the whole batch in one `BEGIN IMMEDIATE` transaction) and `mutable_increment` (atomic
  `json_set` / `json_extract` arithmetic backing the run budget counters), both single-statement
  with no read-then-write gap.

## [0.2.0] - 2026-07-12

SPEC-056 Mission Control, Phase 0A: the mutable directory plane (completing the SPEC-032
risk) — arcstore's first non-insert-once storage, and the atomic single-owner claim primitive
the whole task system is built on.

### Added

- **Mutable directory plane** — a durable `mutable_records(collection, key, value, updated_at,
  PK(collection, key))` table + backend methods (`mutable_write`, `mutable_read`,
  `mutable_delete`, `mutable_query`, `mutable_merge`) alongside the existing insert-once
  operational tables. First consumer is the `tasks` collection (below); the broader SPEC-032
  migration of entities/teams/channels onto this plane stays out of scope for this release.
- **`update_if` — atomic conditional write (the single-owner claim primitive)** — a
  compare-and-swap update over `mutable_records` (`UPDATE ... WHERE collection=? AND key=? AND
  <condition>`) so two writers racing to claim or assign the same record can never both
  succeed. Every `mutable_*` write emits an AU-2/AU-3 audit event, fail-open (AU-5) so a sink
  outage can't block a write.
- **`Task` model + `TaskStore`** (`arcstore.tasks`) — the durable backing for SPEC-056 Mission
  Control: `title`/`description`/`priority`/`owner_did`/`status`/`blocked_by`/`parent_id`/
  `run_id`/`resolution`/`output`, built on the mutable plane's atomic `update_if` so
  `claim_task`/`assign_task` (arcagent) can never double-own a task. `MutableTaskBackend` is the
  Protocol the store depends on, not a concrete backend.

## [0.1.0] - 2026-07-05

Initial release (SPEC-026): the always-on operational/observability data plane other Arc
layers read and write through.

### Added

- **Always-on local spool** (`spool.py`) — `record()` appends a flat, frozen `SpoolRecord` to a
  local file the moment an `arcllm`/`arcrun`/`arcagent` call happens, independent of any running
  store, server, DB, or UI; `read()` iterates records, skipping corrupt lines.
- **`StoreIngest`** (`ingest.py`) — a pure file-tailer that backfills and tails the spool and the
  `arctrust` WORM chain into a queryable backend. Crash-safe via a per-file byte cursor persisted
  in the backend; replay is harmless because every row is keyed by a content-derived id
  (`INSERT OR IGNORE`), so at-least-once ingest never duplicates rows. The WORM is verified on
  ingest (`arctrust.verify_chain`) and each mirrored row carries the `verified` result.
- **`query.py`** — read API over the ingested backend: `recent`, `audit_records`,
  `skill_versions`, `skill_candidate_body`.
- **`StorageBackend` protocol** with `SqliteBackend` (default) and an in-memory backend for
  tests.
- **`ArcStoreConfig` + `resolve_data_dir`** (`config.py`) — the single `[arcstore]` config schema
  and the single Arc data-directory resolution rule (`ARCSTORE_DATA_DIR` env > configured
  `data_dir` > `~/.arc/store` default) shared by every entry point, so a direct `arcllm` call and
  a later `arc agent serve` agree on the same spool/store path.
- **Metadata-only records by default** (`records.py`) — `SpoolRecord` carries no prompt/response
  content unless explicitly opted in.
