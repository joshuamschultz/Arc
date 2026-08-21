# Solution Design Document: Data-Source Ingestion & Routing (arcmemory)

## Context References

- **PRD:** [PRD.md](./PRD.md) · **Decisions:** [D-683..D-705 + Research Insights](../../decisions-log.md)
- **Tech / compliance:** [.claude/steering/tech.md](../../steering/tech.md) (regime `fedramp/nist`) · **Structure:** [.claude/steering/structure.md](../../steering/structure.md)

## Overview

A new arcmemory subsystem that turns a connected source into usable, retrievable knowledge in the right home. A thin **source adapter** normalizes a connector's pushed batch into typed records + a shape hint; a single **Router** applies the operator-approved mapping and fans each record to one-or-more **sinks** — the memory sink (existing capture/consolidate), the **document-search indexer** (extract→chunk→embed→per-source hybrid index, pointer not bytes), or the **datastore registrar** (schema→typed read-only ops). The agent retrieves through three source-scoped tools registered in arcagent (`memory_recall`, `document_search`, `datastore_query`); arcrun only invokes them. Everything crosses arcmemory's **Brain port** as primitives — arcmemory imports neither arcagent, the connectors, nor the scheduler (arch-test guarded). The document index sits behind a pluggable `IndexBackend` (SQLite default, Postgres+pgvector opt-in), mirroring `arcstore.backends`. Living connections: bounded backfill + mutability-driven incremental sync. Every ingest and retrieval is identity-scoped, classification-gated, bounded, audited, and signed.

## Architecture

Flow (connect): connector auth/transport (arcagent-layer, separate project) → `brain.register_source(source, kind, sample)` → arcmemory inspects a sample, **proposes** a source→home mapping (blob/db ontology drafted as Entity+Fact facts) → the proposal lands as a SPEC-035 pending row → operator `arc approve` signs an operator-DID grant → mapping committed as facts. Flow (ingest): connector fetches (owns cursor) → `brain.ingest_batch(source, records)` → arcmemory derives a deterministic `event_id`, re-validates caps, and the **Router** dispatches per the approved mapping → memory sink / doc indexer / datastore registrar. Flow (retrieve): agent calls a tool → arcmemory resolves `source → Scope.key`, runs the home's retrieval (hybrid RRF + optional rerank for docs; typed parameterized query for the datastore; existing recall for memory), gates no-read-up per provenance, returns typed provenance-carrying results → arcrun sends them, unaware. Flow (sync): arcagent's scheduler fires a signed `workflow_run` (no prose) → connector's sync workflow diffs (immutable=append, mutable=upsert/delete) → `ingest_batch`; webhooks call the same path connector-side. Error handling + degrade per `tech.md#error-handling-pattern`.

## Components

### COMP-001: Source adapter + Router (arcmemory)
**Responsibility:** Per-source-type adapter normalizes a pushed batch into typed records + shape hint; one Router applies the approved mapping and fans each record to one-or-more sinks (fan-out capable). No vendor logic in the sinks.
**Dependencies:** Brain port, the approved mapping (COMP-003), the three sinks (COMP-004/006/008). **Inputs:** `(source_id, records, shape_hint)`. **Outputs:** records dispatched to homes; nothing crosses back up. **Decisions:** D-683.

### COMP-002: Brain-port ingestion API (arcmemory)
**Responsibility:** New Brain methods `register_source`, `propose_mapping`, `ingest_batch(source, records)` speaking primitives. Derives `event_id = sha256(source_id + external_id)` (or `content_hash(text)`), makes ingest idempotent via `EpisodicStore.append`'s `INSERT OR REPLACE`, adds `source_updated_at` last-writer-wins ordering, and re-validates the backfill/batch caps zero-trust at the boundary.
**Dependencies:** `stores/episodic.py`, `security.content_hash`, `types.Event` (+ `source_updated_at`). **Inputs:** primitive batches. **Outputs:** idempotent, ordered, capped ingest. **Decisions:** D-684, D-704.

### COMP-003: Mapping proposal, approval & ontology (arcmemory + arcstore)
**Responsibility:** The agent samples a source and proposes a source→home mapping; blob/datastore ontologies are drafted as Entity+Fact projections. The proposal is a SPEC-035 pending row; `arc approve` signs an operator-DID grant. The mapping is stored as facts on a per-source `mapping` Entity so the additive `was:` trail carries re-proposal on shape change.
**Dependencies:** `stores/semantic.py` (Entity/Fact/`was:`), arcstore approvals (SPEC-035), `mdfile.py`. **Inputs:** a source sample. **Outputs:** a signed, committed mapping + ontology facts. **Decisions:** D-691; research.

### COMP-004: Memory sink (arcmemory)
**Responsibility:** Records routed to memory ingest through the existing `capture` + consolidation path (events, outcomes, implicit procedures). No new memory machinery.
**Dependencies:** `capture.py`, `consolidate.py`. **Inputs:** memory-routed records. **Outputs:** episodic events + consolidated cards. **Decisions:** D-688.

### COMP-005: Extractor + Chunker seams (arcmemory)
**Responsibility:** An `Extractor` Protocol (per-type, narrow, pure-Python — `pypdf` text-only, `python-docx`/`openpyxl` XML-only, Tesseract-subprocess for OCR) run **out-of-process, CPU/time/mem-capped**; a `Chunker` Protocol (recursive 512-tok/10% overlap; AST/tree-sitter for code; row-group for tables) emitting the existing `SourceChunk` shape. Text passes `document_sanitize` (COMP-011) before chunking. No all-in-one converter (RCE surface).
**Dependencies:** injected seams (mirror `Embedder`/`Reranker`), `security.document_sanitize`. **Inputs:** a blob body + type. **Outputs:** clean, sized `SourceChunk`s. **Decisions:** D-685; research (LLM03/ASI05).

### COMP-006: Per-source hybrid doc index + `document_search` (arcmemory)
**Responsibility:** Reuse `SurfaceIndex` (vec + BM25 + graph + recency, RRF-fused) per connected source under its own `Scope.key` (`<did>:doc:<source_id>`); **add the missing `chunks.scope` filter to `_vec_search`** so vector search is scope-isolated. `document_search(query, source?, filters?)` resolves source→scope, runs the tier+margin-gated Reranker on the bounded top-K, returns chunk + pointer + provenance. Store pointer, never the bytes.
**Dependencies:** `index/{surface,structural,fusion,rebuild}.py`, COMP-007 backend, COMP-005. **Inputs:** `(query, source?, filters?)`. **Outputs:** ranked chunks + pointers + provenance. **Decisions:** D-685, D-686, D-690; research (vec0 fix).

### COMP-007: Pluggable IndexBackend seam (arcmemory)
**Responsibility:** An arcmemory-owned `IndexBackend` Protocol (async, `@runtime_checkable`, transactions off-contract) + `open_index_backend(backend="sqlite")` factory with `_DEFERRED={"postgres"}` raising `NotImplementedError`. Extract today's inline `sqlite_vec`/FTS SQL out of `surface.py`/`db.py` into `SqliteIndexBackend`; RRF/graph/recency/gate stay above the Protocol. `arcmemory[postgres]` optional extra (lazy import); `PostgresIndexBackend` (pgvector HNSW) opt-in; connection secret via the `arcllm/vault.py::VaultResolver` pattern. Mirror `arcstore.backends`.
**Dependencies:** `db.py`, `arcstore.backends` (pattern), `arcllm.vault` (secret seam). **Inputs:** backend name + config. **Outputs:** a backend the index calls. **Decisions:** D-687, D-702; research.

### COMP-008: Datastore ontology + typed-op generator + `datastore_query` (arcmemory)
**Responsibility:** Introspect a connected DB schema (SQLAlchemy reflection), represent it as `db_table` Entities (Facts: PK, row_count, searchable_columns, FK-as-fact) + an entity-map (`invoice→table:invoices`), and generate bounded typed read-only ops (`get_record`/`find`/`list`) — parameterized, row/time-capped, on a read-only connection, one-hop FK only, no raw SQL. `datastore_query` exposes them.
**Dependencies:** a read-only DB connection (via the connector's transport), `stores/semantic.py` (ontology facts). **Inputs:** `(op, table, args)`. **Outputs:** typed rows + provenance. **Decisions:** D-689.

### COMP-009: Blob ontology discovery walker (arcmemory)
**Responsibility:** Walk a connected blob source and write a **coarse** folder/type/tag catalog as `blob_folder`/`blob_container` Entities+Facts (path, file_count, predominant_type, classification, last_synced) — O(folders/types), bounded by directory cardinality not object count. Feed folder/type names into `tagging.entity_vocabulary`. Per-file pointer/classification detail stays in `chunks` (COMP-006), not duplicated here.
**Dependencies:** `stores/semantic.py`, `tagging.py`, `index/source.py` (chunk shape). **Inputs:** a source listing. **Outputs:** a folder/type ontology + groundable cues. **Decisions:** research; PRD REQ-372.

### COMP-010: Sync engine split (arcmemory boundary + arcagent scheduler)
**Responsibility:** Mutability-driven sync — immutable=append-only, mutable=reconcile (upsert/delete tombstones, never hard delete). Poll cadence is an arcagent `ScheduleEntry(type="interval", action="workflow_run", no prompt)`; webhook receipt + lease renewal (50% TTL) are connector-side. Backfill caps (90d / 10MB-per-object / 5GB-or-50k) enforced before embed and re-validated at `ingest_batch`. arcmemory owns only `ingest_batch` + the per-source mutability flag; it never imports scheduler/connectors.
**Dependencies:** arcagent scheduler (connector-side wiring), COMP-002. **Inputs:** sync triggers → batches. **Outputs:** fresh homes, bounded. **Decisions:** D-688, D-703, D-704; research.

### COMP-011: Ingest security — classification, sanitize, dedup (arcmemory)
**Responsibility:** `classify_remote_object(source_config, item_meta)` by precedence (native→container→fail-closed), content scan only RAISES via `dominating_classification`. `document_sanitize` (chunked, stronger prose patterns, audit-emit-on-match, extended `_defang`). Canonical `content_hash→item` table + additive provenance list; retrieval gates **per-provenance**, not item-max.
**Dependencies:** `security.py`, `arctrust.{classification,redaction,audit}`, `stores/semantic.py`. **Inputs:** ingested items + retrieval candidates. **Outputs:** labeled, defanged, deduped, provenance-tracked items. **Decisions:** D-695, D-696; research (LLM01/02/05, ASI06).

### COMP-012: Retrieval tool registration (arcagent memory module)
**Responsibility:** Register three source-scoped tools in arcagent's registry — `memory_recall` (exists), `document_search`, `datastore_query` — each calling the arcmemory Brain and returning typed provenance-carrying results. arcrun only invokes; source is a filter arg, not a per-source tool.
**Dependencies:** arcagent `modules/memory` (tool decorator), the Brain port. **Inputs:** tool calls. **Outputs:** gated, typed results into the turn. **Decisions:** D-690.

### COMP-013: Security envelope (arcmemory + arctrust)
**Responsibility:** Every ingest batch, mapping op, and retrieval/query carries `caller_did`, passes the 5-layer PolicyPipeline (fail-closed), and emits an `AuditEvent` via the single `arctrust.audit.emit` chokepoint. Connectors hold credentials (vault-backed, never on disk); this layer never sees a raw secret; TLS on fetch; signed connectors.
**Dependencies:** `arctrust.{policy,audit,identity}`, connector supply chain (2026-08-04). **Inputs:** every operation. **Outputs:** identified, authorized, audited operations. **Decisions:** D-692, D-693, D-694, D-697, D-698.

### COMP-014: Degrade + config off-switches (arcmemory config)
**Responsibility:** No embedder→BM25+graph; source offline→serve indexed; unlabeled→fail-closed. Config toggles per capability (doc-search, datastore, per-source sync, backend selector, backfill caps) each returning prior behavior when off.
**Dependencies:** `config.py` (new frozen fields), all paths. **Inputs:** missing-dependency / disabled-config conditions. **Outputs:** graceful degrade or exact prior behavior. **Decisions:** D-700.

### COMP-015: Triad E2E + determinism/boundary guard (tests)
**Responsibility:** Real-path E2E for each axis — Slack (memory), Dropbox (doc-search), SQLite (datastore) — only the connector wire faked, each falsifiable by toggle. Architecture tests: arcmemory imports neither arcagent nor scheduler/connectors; the trigger/rank path takes no model on the deterministic parts.
**Dependencies:** `tests/architecture/*`, the SPEC-071/072 real-path harness. **Inputs:** the real ingestion+retrieval paths. **Outputs:** proof each axis works end-to-end + boundaries hold. **Decisions:** D-701, D-703.

## Data Model

Markdown source-of-truth + disposable SQLite (or Postgres opt-in) index — unchanged ethos. New: `Event.source_updated_at` (ordering) + deterministic ingest `event_id`; a persisted canonical `content_hash → item_id` + `provenances[]` table in `MemoryDB` (replacing the in-memory 128-ring for cross-source dedup); per-source doc chunks under a distinct `Scope.key` with a **scope column/filter added to `vec0`**; ontology as `blob_folder`/`blob_container`/`db_table` Entities (glass-box markdown); mapping as facts on a `mapping` Entity with the existing `was:` trail. No blob body or DB row is copied into arcmemory — only chunks+pointers (docs) and schema facts (datastore). The `IndexBackend` keeps the same conceptual schema (`scope, chunk_id, text, embedding, content_hash`) across SQLite and Postgres so a future move is a swap.

## External Integrations

Reuses arctrust (classification/policy/audit/identity), arcstore (SPEC-035 approvals; and the `backends` Protocol *pattern*, not its tables), the arcllm-backed embedder/reranker seams, and arcagent's scheduler (connector-side, via signed `workflow_run`). New optional dependency: `arcmemory[postgres]` (`psycopg`/`asyncpg` + `pgvector`) — lazy, opt-in, never on the default path. Extraction libraries (`pypdf`, `python-docx`, `openpyxl`, Tesseract) are per-type, sandboxed. Connectors (auth/transport/webhook) are the 2026-08-04 project — consumed, not built.

## Traceability

| Requirement | Components |
|---|---|
| REQ-363 | COMP-001, COMP-002 |
| REQ-364 | COMP-001 |
| REQ-365 | COMP-003 |
| REQ-366 | COMP-004 |
| REQ-367 | COMP-005, COMP-006 |
| REQ-368 | COMP-006 |
| REQ-369 | COMP-006, COMP-007 |
| REQ-370 | COMP-008 |
| REQ-371 | COMP-008, COMP-012 |
| REQ-372 | COMP-003, COMP-009 |
| REQ-373 | COMP-010 |
| REQ-374 | COMP-010 |
| REQ-375 | COMP-002, COMP-010 |
| REQ-376 | COMP-007 |
| REQ-377 | COMP-006, COMP-007 |
| REQ-378 | COMP-012 |
| REQ-379 | COMP-011 |
| REQ-380 | COMP-005, COMP-011 |
| REQ-381 | COMP-013 |
| REQ-382 | COMP-011 |
| REQ-383 | COMP-014 |
| REQ-384 | COMP-015 |
| REQ-385 | COMP-013 |

## Threat Mapping (fedramp/nist — required)

| Threat | Mitigation | Component |
|---|---|---|
| **LLM01 Prompt injection** (malicious document body) | `document_sanitize` + boundary-mark as DATA; never system-role; content never gains tool authority | COMP-005, COMP-011 |
| **LLM02 / ASI03 Sensitive-info / identity abuse** | no-read-up per provenance; classification precedence, fail-closed at federal | COMP-011, COMP-013 |
| **LLM03 / ASI04 Supply chain** | signed connectors; narrow per-type extractors (no all-in-one converter); pip-audit | COMP-005, COMP-013 |
| **ASI05 Unexpected code execution** | extractors out-of-process, resource-capped; no macro/OLE engines | COMP-005 |
| **LLM05 Improper output / injection into DB** | typed, parameterized, read-only lookup ops; no agent/generated SQL | COMP-008 |
| **LLM07 System-prompt / secret leakage** | connectors hold vault-backed creds; this layer never sees a raw secret | COMP-013 |
| **LLM10 Unbounded consumption** | backfill windowed/capped enforced before embed; batch rate caps | COMP-002, COMP-010 |
| **ASI06 Memory / context poisoning** | sanitize + additive provenance; content-hash idempotency defeats replay flooding | COMP-011 |

## Alternatives Considered

A1: per-home ingesters / connector-pushes-to-homes (REJECTED — routing scatters / boundary reverses; D-683). A2: injected SourceReader pull / arcstore spool (REJECTED for v1 — push seam mirrors capture/on_moment; D-684). A3: metadata-only or full-mirror doc index (REJECTED — no semantic search / violates "never the bytes"; D-685). A4: reuse index as-is / new engine (REJECTED — floods memory recall / second engine; shared-engine-separate-pool chosen; D-686). A5: Postgres-primary now / SQLite-forever (REJECTED — breaks run-anywhere / misses the roadmap; pluggable seam; D-687). A6: agent-authored SQL / NL→SQL (REJECTED — injection + LLM compile surface; typed ops; D-689). A7: per-source generated tools / unified merger (REJECTED — registry churn / blurred provenance; three source-scoped tools; D-690). A8: federate to native search (DEFERRED — classification-passthrough unsolved; always-re-index v1; D-688).

## Risks & Mitigations

Un-scoped `vec0` slows every agent's recall (mitigation: the seam refactor + scope filter is in-scope, COMP-006/007). All-in-one extractor RCE (mitigation: narrow sandboxed per-type extractors, COMP-005). Document prompt injection (mitigation: `document_sanitize`, DATA framing, no tool authority — risk-reduction, COMP-011). Unbounded backfill (mitigation: caps before embed, COMP-002/010). Over/under classification on dedup (mitigation: per-provenance gating, COMP-011). Scope creep across three axes (mitigation: the triad first-proof proves each on the real path, COMP-015). Postgres opt-in coupling to Supabase (mitigation: target generic Postgres+pgvector; Supabase auth is a separate optional layer, COMP-007).

## Open Questions

- Federation classification-passthrough contract (fast-follow) — the `FederatedRecallAdapter` shape.
- A "where should I look?" router hint vs the agent's own tool-selection.
- Concrete per-source backfill-cap overrides surfaced in the SPEC-035 approval artifact.
