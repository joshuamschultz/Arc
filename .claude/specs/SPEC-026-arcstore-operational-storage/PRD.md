# SPEC-026 — Arcstore Operational Storage: Product Requirements

## Problem Statement

arcui is supposed to auto-tap and display running agent histories. It frequently **misses** events and **fails to update**. Root-cause analysis surfaced two structural faults:

1. **No durable operational store.** Operational/observability data (LLM calls, runs, agent status, token/cost, traces) is written ad hoc per package, with no single durable home the UI can read or reconcile against.
2. **A fragile parallel push channel.** `arcui.bridge.UIBridgeSink` receives events live from `arctrust.audit.emit()`. Because `emit()` swallows sink failures (AU-5), any drop on this channel is silent. When arcui is down, the bridge breaks, or a frame is lost, those events never reach the UI and **nothing reconciles them**.

Compounding this, `arctrust` has **no durable WORM today**: `JsonlSink` persists but is unchained/tamperable; `SignedChainSink` is chained + signed but keeps records **in memory only** and evaporates on process exit. Neither alone is a compliance-grade write-once record.

There is also a missing capability: a developer should be able to run a direct `arcllm` call from the CLI with nothing else configured, and **later** spin up the store/UI and see that call. Today the call leaves no durable operational trace.

## Vision

> **Every Arc action is durably recorded the instant it happens — regardless of whether any server, DB, or UI is running — and the UI reads that durable record, never a live wire that can drop.** Compliance gets a real tamper-evident WORM in the security nucleus. Observability is ambient: call now, spin up the store later, see the full history.

## Audience

| Persona | Pain today | What this fixes |
|---|---|---|
| **Operator watching agents (arcui)** | UI misses events, silently stalls, shows stale state | UI reads an always-populated durable store that backfills on startup; nothing to drop |
| **Developer using arcllm directly** | A direct CLI call leaves no trace; no way to review past calls | Spool records every call on-by-default; bring up arcstore later to see history |
| **Compliance / federal evaluator (DOE/NASA)** | No durable tamper-evident audit log; signed chain is in-memory only | Durable append-only Ed25519-signed hash chain, always on at every tier; cloud WORM-at-rest option |
| **Platform owner (Josh)** | Too many failure surfaces; a push channel that "constantly" misses | One write path per concern, one read path, zero parallel wires. Deleted code, not added |
| **Future contributors** | Unclear where operational data belongs; each package reinvents storage | One `StorageBackend` Protocol; clear arctrust(WORM) vs arcstore(operational) boundary |

## Use Cases

### UC-1 — Call now, see later
Developer runs `arc llm "summarize this"` from the CLI. No store, no UI, no server is running. The call completes and is durably recorded to the spool. An hour later the developer runs `arc store up` (or opens arcui); arcstore backfills from the spool and the prior call appears in full (prompt metadata, model, tokens, cost, latency, outcome).

### UC-2 — UI never misses
An agent runs a 40-step task while arcui is closed. The operator opens arcui afterward. Every step is present, because the run was recorded to the spool/WORM as it happened and arcstore backfilled on startup. No event was contingent on the UI being connected.

### UC-3 — Reconnect with no gap
arcui is open during a live run. The browser tab sleeps, the connection drops, the server restarts. On return, the UI re-reads the store (which kept ingesting the durable files) and shows the complete, current state. There is no seq-skew because there is no separate push stream to fall out of sync.

### UC-4 — Tamper-evident compliance log
A federal evaluator runs an agent, then runs `arc audit verify`. arctrust re-reads the durable signed chain from disk and confirms every entry's hash links and signature. Modifying any line on disk makes verification fail. The chain survived process restarts because it is persisted, not in-memory.

### UC-5 — Backend swap without losing the guarantee
An enterprise deployment switches the operational backend from SQLite to Postgres by config. The compliance guarantee is unaffected because the signed chain lives in the arctrust WORM file, not the DB. The arcstore mirror is rebuilt from the durable files against the new backend.

### UC-6 — Air-gapped federal box
A Sandia evaluator deploys on an air-gapped Linux host. arcstore runs with `SqliteBackend` locally; the WORM writes to a local append-only file with optional cloud-WORM disabled. Audit chain and operational history are identical to a commercial deployment — no internet, no external service.

## Functional Requirements

### FR-1 — arctrust durable WORM (P0)

Collapse `JsonlSink` and `SignedChainSink` into a single **durable, append-only, Ed25519-signed hash-chained** audit log.

- Each record persists to an append-only file as one JSON line: `{event, prev_hash, event_hash, signature}`.
- The hash chain links each entry to the previous (`event_hash = sha256(prev_hash + canonical_event_json)`); the entry hash is Ed25519-signed.
- On startup the chain tip is restored from the file tail (chain survives restart).
- `verify_chain()` reads the file and validates every link + signature.
- On by default at **every tier**; tier sets stringency (federal = FIPS crypto + stricter retention), never existence.
- The in-memory-only `SignedChainSink` and the unchained `JsonlSink` are **deleted** (no shim).

**Acceptance criteria:**
- AC-1.1 — Writing N events then constructing a fresh WORM from the same file yields a tip equal to the original and `verify_chain()==True`. Chain survives process restart. **Pillar: Security, Simplicity.**
- AC-1.2 — Mutating any byte of any persisted line makes `verify_chain()==False`. **Pillar: Security.**
- AC-1.3 — A forged line with a recomputed `event_hash` but invalid signature makes `verify_chain()==False`. **Pillar: Security.**
- AC-1.4 — `emit()` to the WORM never raises into the caller; a write failure is logged and swallowed (AU-5). **Pillar: Security, Scalability.**
- AC-1.5 — `arctrust` imports no other Arc package (verified by import-graph test). **Pillar: Modularity.**
- AC-1.6 — `verify_chain(public_key)` validates the **Ed25519 signature** of every record (not only hash links); a record with a valid hash link but invalid signature fails. *(Closes existing defect C1: signatures are currently written but never verified.)* **Pillar: Security.**
- AC-1.7 — each record carries a monotonic `seq` included in the hash; a `seq` gap (end-truncation of a valid prefix) makes verification fail. **Pillar: Security.**
- AC-1.8 — a single `WormSink` instance is enforced per file via `flock`; a torn last line on restart is truncated and a signed `recovery` record appended (never silent). **Pillar: Security.**

### FR-2 — arcstore.spool always-on local recorder (P0)

A server-independent, dependency-light append-only recorder for operational telemetry.

- `arcstore.spool.record(event)` appends one JSON line to a spool file under the shared Arc data dir.
- Depends only on stdlib + pydantic; importing it pulls **no** DB/cloud driver.
- Writing succeeds with no store, server, DB, or UI running.
- Fail-open: a spool write error is logged and swallowed, never raised into the calling path.
- Records carry at minimum: `kind` (llm_call/run_event/agent_event), `actor_did`, `ts`, `request_id`, and a `kind`-specific payload (model, tokens, cost, latency, outcome for llm_call).

**Acceptance criteria:**
- AC-2.1 — `record()` writes a durable line with only the spool imported (no backend, no server). **Pillar: Simplicity, Modularity.**
- AC-2.2 — Importing `arcstore.spool` does not import `sqlite3`-backed store, `psycopg`, or any cloud SDK (import-graph test). **Pillar: Scalability.**
- AC-2.3 — A raised exception inside the underlying write is swallowed and logged; the caller proceeds. **Pillar: Security, Scalability.**
- AC-2.4 — Spool write adds < 5 ms p95 overhead to a no-op call and does not block the event loop. **Pillar: Scalability.**

### FR-3 — StorageBackend Protocol + SqliteBackend with backfill (P0)

A pluggable backend that ingests the durable files into queryable tables.

- `StorageBackend` Protocol defines the seam (upsert/query/transaction); no SQLite specifics leak into the Protocol.
- `SqliteBackend` (WAL) is the default.
- On startup, the store **backfills** from both the spool and the arctrust WORM (everything recorded while the store was down), then **tails** them for new lines.
- arcstore is a **pure file-tailer**: it owns no sink in `emit()` and no live wire.
- Ingestion is idempotent (re-running backfill does not duplicate rows; keyed by record identity).

**Acceptance criteria:**
- AC-3.1 — Records written to the spool/WORM while the store is down appear in queryable tables after the store starts (backfill). **Pillar: Simplicity, Security.**
- AC-3.2 — Lines appended while the store is running appear via tailing within the configured interval. **Pillar: Scalability.**
- AC-3.3 — Running backfill twice over the same files produces no duplicate rows. **Pillar: Simplicity.**
- AC-3.4 — The Protocol has a second in-memory/fake implementation used in tests, proving no SQLite leakage. **Pillar: Modularity.**

### FR-4 — Ambient auto-instrumentation (P0)

arcllm and arcrun record to the spool by default.

- arcllm's client emits an `llm_call` spool record on every completion (success and error), capturing model, token usage, cost, latency, outcome.
- arcrun's loop emits `run_event` records at run start/step/finish.
- Recording is **on by default**, disabled only by explicit config (`arcstore.enabled = false`).
- Producers do not import the backend — only `arcstore.spool`.

**Acceptance criteria:**
- AC-4.1 — A direct arcllm call with default config produces a spool `llm_call` record with token/cost/latency populated. **Pillar: Simplicity, Modularity.**
- AC-4.2 — Setting `arcstore.enabled = false` produces no spool writes. **Pillar: Modularity.**
- AC-4.3 — arcllm/arcrun import `arcstore.spool` only, not the store/backends (import-graph test). **Pillar: Modularity, Scalability.**
- AC-4.4 — An arcllm call that raises still records an `llm_call` with `outcome="error"`. *(Closes defect C3: TelemetryModule has no try/finally today.)* **Pillar: Security.**
- AC-4.5 — `SpoolRecord` stores **metadata only** (no prompt/response text). `store_raw_bodies` defaults to **`False`**; enabling raw capture logs a startup warning. *(Closes defect C2: default is `True` today — wrong for federal.)* **Pillar: Security.**

### FR-5 — arcui/arctui read-only; delete UIBridgeSink (P0)

- `arcui.bridge.UIBridgeSink` and its bridge plumbing are **deleted**.
- The `emit()` fan-out collapses to the single WORM sink.
- arcui/arctui read operational + audit data from the arcstore query layer (poll, pause-on-inactive when the tab/UI is idle).
- No code path makes the UI a sink or subscriber of `emit()`.

**Acceptance criteria:**
- AC-5.1 — No reference to `UIBridgeSink` remains in the codebase (grep test). **Pillar: Simplicity.**
- AC-5.2 — arcui renders agent history by reading the store; killing/restarting the arcui server loses no history (it re-reads the durable store). **Pillar: Simplicity, Scalability.**
- AC-5.3 — Polling pauses when the UI is inactive and resumes on focus. **Pillar: Scalability.**
- AC-5.4 — `arctrust.audit.emit()` has exactly one default sink (the WORM) and no UI coupling (import-graph test). **Pillar: Modularity.**

### FR-6 — CLI lifecycle: starting/creating an agent spins up arcstore (P0)

Agent lifecycle commands make arcstore **ambient** — no separate manual step to get observability.

- `arc agent create` scaffolds an `[arcstore]` block (enabled, defaults) into the new agent's TOML, and creates the store data dir + spool idempotently.
- `arc agent serve`, `arc agent run`, and `arc team` (multi-agent) **spin up arcstore before the agent loop**: the spool dir is always created; if `[arcstore].enabled` with a configured backend, `StoreIngest` (backfill → tail) starts as a managed background task and stops cleanly on shutdown.
- Spin-up is **fail-open**: if the store backend cannot start, the spool still records (the call-now-see-later guarantee holds) and the agent starts anyway. Store failure never blocks the agent.
- An explicit `arc store` command group (`up`, `status`, `verify`, `backfill`) gives manual/air-gapped control, including `verify` (runs `arctrust.verify_chain`) and `backfill` (re-ingest from spool + WORM).

**Acceptance criteria:**
- AC-6.1 — `arc agent create` writes an `[arcstore]` block and creates the data dir + spool file. **Pillar: Simplicity, Modularity.**
- AC-6.2 — `arc agent serve`/`run` and `arc team` start `StoreIngest` and stop it on shutdown (no orphaned tasks). **Pillar: Modularity, Scalability.**
- AC-6.3 — With a deliberately broken backend, the agent still starts and the spool still records; `arc store status` reports the store as degraded. **Pillar: Security, Scalability.**
- AC-6.4 — `arc store verify` returns non-zero on a tampered WORM file. **Pillar: Security.**

### FR-7 — TOML config: arcllm / arcrun / arccli set, use, and create arcstore (P0)

A single canonical config schema, shared — not redefined per package.

- `arcstore.config.ArcStoreConfig` (Pydantic) is the **one** schema. arcllm (`config.toml` / `GlobalConfig`), arcrun config, and arcagent/arccli (`[arcstore]` under `ARCAGENT_`) all reference it — no divergent copies.
- `[arcstore]` keys: `enabled` (default `true`), `data_dir`, `backend` (default `"sqlite"`), `store_raw_bodies` (default `false`, FR-4), `rotation` (default `daily`), `retention`, `sample_rate` (default `1.0`).
- **Data-dir resolution is identical across all three entry points** (precedence: `ARCSTORE_DATA_DIR` env > `[arcstore].data_dir` > default `~/.arc/store`). This is what makes "call a direct arcllm from the CLI, then open arcui" land in the **same** spool/store. Divergent paths would silently fragment history.
- `set` (write a block via `arc agent create`/`arc store init`), `use` (every entry point reads the same resolved config), `create` (init data dir + DB idempotently) are all supported.

**Acceptance criteria:**
- AC-7.1 — arcllm, arcrun, and arccli all resolve the same `data_dir` from identical config + env precedence (shared resolver test). **Pillar: Modularity, Simplicity.**
- AC-7.2 — A direct `arc llm` call and a later `arc agent serve` read/write the same spool path → the CLI call appears in the agent's store (UC-1). **Pillar: Simplicity.**
- AC-7.3 — `ArcStoreConfig` is defined once in `arcstore.config`; import-graph test shows arcllm/arcrun/arcagent import it rather than redefining. **Pillar: Modularity.**
- AC-7.4 — `[arcstore].enabled=false` in any toml disables spool + store for that entry point. **Pillar: Modularity.**

## Non-Functional Requirements

- **NFR-1 (Simplicity)** — The spool record schema is a single flat Pydantic model serialized to one JSON line. No nested envelopes.
- **NFR-2 (Scalability)** — Importing the spool path keeps cold start < 500 ms and baseline memory < 50 MB; DB/cloud drivers load only when the store is run.
- **NFR-3 (Security)** — Spool and WORM writes are fail-open (AU-5): they never propagate an exception into the audited/called path.
- **NFR-4 (Modularity)** — Import direction is strictly one-directional: `arctrust ← arcstore ← {arcllm, arcrun, arcagent, arcui, arctui}`. Enforced by an import-graph test.
- **NFR-5 (Security)** — Durable WORM file is created with owner-only permissions (0600) and an append-only open mode.
- **NFR-6 (Scalability)** — Backend Protocol must support a future change-notify path (**`PRAGMA data_version`** poll → SSE signal, or Postgres `LISTEN/NOTIFY`; *not* `sqlite3_update_hook` which is same-connection only) without changing producer or UI-read contracts.
- **NFR-7 (Security)** — Encryption-at-rest (SC-28) for spool/WORM is a known gap. The metadata-only default (AC-4.5) bounds exposure; for SCIF/CUI sessions deployment must use an encrypted filesystem (LUKS) or per-line vault-key encryption. Documented, not silently assumed.
- **NFR-8 (Modularity)** — Each instance owns its **own** SQLite file (shared-nothing). A shared DB file across instances is prohibited (it produces `SQLITE_BUSY` storms above ~2–3 concurrent writers).

## Out of Scope (this spec)

- Postgres, Supabase, and cloud WORM-at-rest backends — deferred (D-009); the Protocol is built and proven with SQLite only.
- DB-native change-notify push to the browser — deferred until polling latency is a measured problem (NFR-6 keeps the door open).
- Token-by-token LLM streaming into the UI — a separate dedicated stream off arcllm, not the observability path.
- Migration of historical ad hoc per-package data — local-only repo, no backfill of legacy stores (per no-backward-compat directive).

## Traceability

| Requirement | Decisions | NIST control |
|---|---|---|
| FR-1 | D-002, D-003, D-004 | AU-9, AU-10, AU-11 |
| FR-2 | D-005, D-010, D-011 | AU-4, AU-5 |
| FR-3 | D-006, D-009 | AU-4, SC-28 (future backend) |
| FR-4 | D-008 | AU-2, AU-3 |
| FR-5 | D-007 | AU-6 (review) |
