# Implementation Plan: Data-Source Ingestion & Routing (arcmemory)

## Context References

- **PRD:** [PRD.md](./PRD.md) · **SDD:** [SDD.md](./SDD.md) · **Decisions:** D-683..D-705 (+ Research Insights)
- **Tech:** [.claude/steering/tech.md](../../steering/tech.md) (fedramp/nist) · **Structure:** [.claude/steering/structure.md](../../steering/structure.md)

## Phase 1: Foundation — seam, backend refactor, config

- [ ] **T-1018**: (red) Brain-port ingestion API contract + idempotency + ordering
  - domain: test · Components: COMP-002 · Requirements: REQ-363, REQ-375
  - Acceptance: Tests drive `register_source`/`propose_mapping`/`ingest_batch` on the real Brain: a duplicate batch with the same deterministic `event_id=sha256(source+external_id)` upserts once; an out-of-order record with an older `source_updated_at` is a no-op; an over-cap batch is rejected at the boundary. Fails before impl.
- [ ] **T-1019**: (green) Implement Brain-port ingestion API
  - domain: backend · Components: COMP-002 · Requirements: REQ-363, REQ-375
  - Acceptance: `ingest_batch` derives the deterministic id, reuses `EpisodicStore.append` INSERT-OR-REPLACE, adds `Event.source_updated_at`, re-validates caps. T-1018 passes; mypy --strict + ruff clean; arcmemory no-arcagent-import test green.
- [ ] **T-1020**: (red) IndexBackend conformance + SqliteIndexBackend parity + vec0 scope isolation
  - domain: test · Components: COMP-007, COMP-001 · Requirements: REQ-376, REQ-369
  - Acceptance: A parametrized conformance suite (`["sqlite"]`, postgres xfail w/o URL) proves no backend-specific type leaks; a vector search over source A's pool returns zero of source B's chunks (scope-isolated). Fails before impl (vec0 has no scope filter today).
- [ ] **T-1021**: (green) Extract SqliteIndexBackend + vec0 scope filter + factory
  - domain: db · Components: COMP-007 · Requirements: REQ-376, REQ-377, REQ-369
  - Acceptance: Inline `sqlite_vec`/FTS SQL moves out of `surface.py`/`db.py` into `SqliteIndexBackend`; `_vec_search` joins `chunks` and filters `scope`; `open_index_backend` factory with `_DEFERRED={"postgres"}`. RRF/graph/recency/gate unchanged. T-1020 + existing surface/retrieve tests pass; mypy + ruff clean.
- [ ] **T-1022**: (red) Config: new fields + per-capability off-switches
  - domain: test · Components: COMP-014 · Requirements: REQ-383
  - Acceptance: Tests assert frozen fields exist with defaults — `doc_chunk_tokens`(512)/`doc_chunk_overlap`/`doc_rerank_margin`, backfill caps, `index_backend`("sqlite"), and toggles for doc-search/datastore/per-source-sync — and each disabled toggle is representable. Fails before impl.
- [ ] **T-1023**: (green) Add config fields + degrade wiring
  - domain: backend · Components: COMP-014 · Requirements: REQ-383
  - Acceptance: Frozen fields added with tier defaults; no-embedder→BM25+graph and disabled-toggle→prior-behavior wired. T-1022 passes; existing config tests green; mypy + ruff clean.
- [ ] **T-1024**: (red) Source adapter + Router fan-out
  - domain: test · Components: COMP-001 · Requirements: REQ-364
  - Acceptance: Tests: a record with a mapping routing it to memory+doc-search reaches BOTH sinks (fan-out); a mapping to datastore-only reaches only the registrar; unmapped source is inert. Fails before impl.
- [ ] **T-1025**: (green) Implement adapter seam + Router
  - domain: backend · Components: COMP-001 · Requirements: REQ-364
  - Acceptance: Thin per-source adapter normalizes to typed records; Router dispatches per approved mapping, fan-out capable. T-1024 passes; arch tests green; mypy + ruff clean.

## Phase 2: Core — the three axes

- [ ] **T-1026**: (red) Memory sink routes through capture
  - domain: test · Components: COMP-004 · Requirements: REQ-366
  - Acceptance: A memory-routed record becomes an episodic event and is recallable via the existing path; no new memory machinery invoked. Fails before impl.
- [ ] **T-1027**: (green) Implement memory sink
  - domain: backend · Components: COMP-004 · Requirements: REQ-366
  - Acceptance: Memory sink calls `capture`/consolidation; T-1026 passes; SPEC-041/071/072 memory tests stay green; mypy + ruff clean.
- [ ] **T-1028**: (red) Extractor + Chunker seams (per-type, sandboxed, sizes)
  - domain: test · Components: COMP-005 · Requirements: REQ-367, REQ-380
  - Acceptance: Tests: a PDF/docx/markdown/code sample extracts via its narrow extractor (no all-in-one converter imported); chunks are 512-tok/10%-overlap (recursive), code chunks respect AST node boundaries; text passes `document_sanitize` before chunking. Fails before impl.
- [ ] **T-1029**: (green) Implement Extractor + Chunker
  - domain: backend · Components: COMP-005 · Requirements: REQ-367, REQ-380
  - Acceptance: `Extractor` Protocol (pypdf text-only, python-docx/openpyxl XML-only, Tesseract subprocess) run out-of-process + capped; `Chunker` Protocol emitting `SourceChunk` shape. T-1028 passes; no macro/OLE engine; mypy + ruff clean.
- [ ] **T-1030**: (red) Per-source hybrid doc index + document_search
  - domain: test · Components: COMP-006 · Requirements: REQ-367, REQ-368, REQ-369
  - Acceptance: Tests: a Dropbox-shaped file set indexes into its own `<did>:doc:<source>` scope; `document_search(query, source?)` returns hybrid-ranked chunks + pointers (never the body); a query scoped to source A never returns source B; results carry provenance. Fails before impl.
- [ ] **T-1031**: (green) Implement doc indexer + document_search
  - domain: db · Components: COMP-006 · Requirements: REQ-367, REQ-368, REQ-369
  - Acceptance: Reuse SurfaceIndex per Scope over the IndexBackend; tier+margin Reranker on bounded top-K; pointer-not-bytes. T-1030 passes; mypy + ruff clean.
- [ ] **T-1032**: (red) Datastore ontology + typed read-only ops + datastore_query
  - domain: test · Components: COMP-008 · Requirements: REQ-370, REQ-371
  - Acceptance: Tests: schema introspection yields `db_table` facts + entity-map; `get_record(invoices,"001")` returns the row; a `find` is parameterized + row-capped; there is NO code path that executes agent-supplied SQL; joins follow at most one FK hop. Fails before impl.
- [ ] **T-1033**: (green) Implement schema introspection + typed-op generator + datastore_query
  - domain: db · Components: COMP-008 · Requirements: REQ-370, REQ-371
  - Acceptance: SQLAlchemy reflection → typed ops on a read-only connection, row/time-capped; ontology facts persisted. T-1032 passes; mypy + ruff clean.
- [ ] **T-1034**: (red) Blob ontology walker (bounded, coarse)
  - domain: test · Components: COMP-009 · Requirements: REQ-372
  - Acceptance: Tests: walking a source writes `blob_folder`/`blob_container` Entities+Facts (path/file_count/predominant_type/classification); the ontology size is O(folders/types), not O(objects); folder/type names become tagging cues. Fails before impl.
- [ ] **T-1035**: (green) Implement blob ontology discovery walker
  - domain: backend · Components: COMP-009 · Requirements: REQ-372
  - Acceptance: Coarse folder/type/tag catalog reusing SemanticStore + tagging; per-file detail stays in chunks. T-1034 passes; mypy + ruff clean.
- [ ] **T-1036**: (red) Classification discovery + document_sanitize + provenance dedup
  - domain: test · Components: COMP-011 · Requirements: REQ-379, REQ-380, REQ-382
  - Acceptance: Tests: an unlabeled item inherits the container label, else fails closed (federal); a content-scan hit only RAISES the label; an injection-pattern match in a document emits an audit event AND is defanged; the same bytes from Slack + Dropbox form ONE canonical item with two provenances, gated per-provenance not item-max. Fails before impl.
- [ ] **T-1037**: (green) Implement classify_remote_object + document_sanitize + provenance table
  - domain: backend · Components: COMP-011 · Requirements: REQ-379, REQ-380, REQ-382
  - Acceptance: Precedence classifier, chunked audit-emitting sanitizer, persisted content-hash→item + provenances, per-provenance gate. T-1036 passes; security tests green; mypy + ruff clean.

## Phase 3: Integration — mapping, sync, tools, envelope

- [ ] **T-1038**: (red) Mapping proposal + SPEC-035 approval + mapping-as-facts
  - domain: test · Components: COMP-003 · Requirements: REQ-365, REQ-372
  - Acceptance: Tests (real approval path): `propose_mapping` writes a SPEC-035 pending row; ingest does NOT commit until an operator-DID grant signs it; a re-proposal on a changed source records a `was:` trail on the mapping Entity. Fails before impl.
- [ ] **T-1039**: (green) Implement propose_mapping + approval wiring + mapping facts
  - domain: backend · Components: COMP-003 · Requirements: REQ-365, REQ-372
  - Acceptance: Sampled proposal → arcstore pending → grant-gated commit; mapping stored as facts. T-1038 passes; SPEC-035 tests stay green; mypy + ruff clean.
- [ ] **T-1040**: (red) Mutability-driven sync + backfill caps
  - domain: test · Components: COMP-010 · Requirements: REQ-373, REQ-375
  - Acceptance: Tests: an immutable source append-onlys (no reconciliation pass); a mutable source applies an update (upsert) and a delete (tombstone, never hard-delete); a backfill over the cap stops with an audit + resumable status BEFORE embedding the over-cap objects. Fails before impl.
- [ ] **T-1041**: (green) Implement sync engine + cap enforcement + trigger seam
  - domain: backend · Components: COMP-010 · Requirements: REQ-373, REQ-374, REQ-375
  - Acceptance: Per-source mutability flag drives append vs reconcile; caps enforced before embed; the poll trigger is a signed `workflow_run` contract (connector-side wiring), arcmemory imports no scheduler/connector. T-1040 passes; boundary tests green; mypy + ruff clean.
- [ ] **T-1042**: (red) Three source-scoped retrieval tools registered
  - domain: test · Components: COMP-012 · Requirements: REQ-378
  - Acceptance: Tests: `memory_recall`, `document_search`, `datastore_query` are registered in arcagent's registry, each returns typed provenance-carrying results, and a `source` arg scopes the call (no per-source tool explosion). Fails before impl.
- [ ] **T-1043**: (green) Register the three tools in the arcagent memory module
  - domain: backend · Components: COMP-012 · Requirements: REQ-378
  - Acceptance: Tools call the Brain, return typed results; arcrun only invokes. T-1042 passes; existing memory-module tests green; mypy + ruff clean.
- [ ] **T-1044**: (red) Security envelope on every op
  - domain: test · Components: COMP-013 · Requirements: REQ-381, REQ-385
  - Acceptance: Tests: every ingest/mapping/retrieval/query carries `caller_did`, passes the PolicyPipeline (a denied op fails closed), and emits an `AuditEvent`; no raw secret is ever passed to this layer. Fails before impl.
- [ ] **T-1045**: (green) Wire identity + policy + audit on all paths
  - domain: backend · Components: COMP-013 · Requirements: REQ-381, REQ-385
  - Acceptance: caller_did threaded; policy-gated; single audit chokepoint; secrets connector-held. T-1044 passes; security + arch tests green; mypy + ruff clean.

## Phase 4: Polish — triad E2E, boundaries, degrade

- [ ] **T-1046**: (red) Triad real-path E2E (all three axes)
  - domain: test · Components: COMP-015 · Requirements: REQ-384
  - Acceptance: Through a real agent run (only the connector wire faked): (A) a Slack backfill becomes recallable memory; (B) a Dropbox file is found by `document_search` and its pointer resolves, body never stored; (C) a SQLite `datastore_query` returns invoice 001 exactly; every surfaced result is classification-gated + audited. Each axis falsifiable by disabling its toggle. Fails before impl.
- [ ] **T-1047**: (green) Make the triad green end-to-end
  - domain: backend · Components: COMP-015 · Requirements: REQ-384
  - Acceptance: The three axes pass on the real path; T-1046 passes; full arcmemory + arcagent-memory suites stay green; mypy + ruff clean.
- [ ] **T-1048**: (red) Boundary + determinism guard
  - domain: test · Components: COMP-015 · Requirements: REQ-374, REQ-377
  - Acceptance: Tests: arcmemory imports neither arcagent, the scheduler, nor connector packages; the ingest trigger/rank deterministic parts take no model seam; backend swap requires no fusion/gate change (poison-object seam). Fails before impl if a path reaches upward or to a model.
- [ ] **T-1049**: (green) Enforce boundaries + determinism
  - domain: backend · Components: COMP-015 · Requirements: REQ-374, REQ-377
  - Acceptance: Boundaries clean; deterministic paths model-free. T-1048 passes; architecture + dependency-boundary tests green.
- [ ] **T-1050**: (refactor) Degrade + hardening + off-switch verification
  - domain: backend · Components: COMP-014 · Requirements: REQ-383
  - Acceptance: Every new path degrades not crashes (no embedder / source offline / no label); each capability's disable toggle returns exact prior behavior; caps + gating + audit hold on every axis. All SPEC-073 + prior memory tests green; ruff + mypy --strict + architecture tests clean.

## Traceability

| Requirement | Tasks |
|---|---|
| REQ-363 | T-1018, T-1019, T-1046 |
| REQ-364 | T-1024, T-1025 |
| REQ-365 | T-1038, T-1039 |
| REQ-366 | T-1026, T-1027, T-1046 |
| REQ-367 | T-1028, T-1029, T-1030, T-1031 |
| REQ-368 | T-1030, T-1031 |
| REQ-369 | T-1020, T-1021, T-1030, T-1031 |
| REQ-370 | T-1032, T-1033 |
| REQ-371 | T-1032, T-1033 |
| REQ-372 | T-1034, T-1035, T-1038, T-1039 |
| REQ-373 | T-1040, T-1041 |
| REQ-374 | T-1041, T-1048, T-1049 |
| REQ-375 | T-1018, T-1019, T-1040, T-1041 |
| REQ-376 | T-1020, T-1021 |
| REQ-377 | T-1021, T-1048, T-1049 |
| REQ-378 | T-1042, T-1043 |
| REQ-379 | T-1036, T-1037 |
| REQ-380 | T-1028, T-1029, T-1036, T-1037 |
| REQ-381 | T-1044, T-1045 |
| REQ-382 | T-1036, T-1037 |
| REQ-383 | T-1022, T-1023, T-1050 |
| REQ-384 | T-1046, T-1047 |
| REQ-385 | T-1044, T-1045 |

## Open Questions

- Federation classification-passthrough contract (`FederatedRecallAdapter`) — deferred fast-follow.
- A "where should I look?" router hint vs the agent's own tool-selection.
- Per-source backfill-cap overrides surfaced in the SPEC-035 approval artifact.
