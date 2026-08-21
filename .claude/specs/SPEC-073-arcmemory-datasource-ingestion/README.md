# Specification: SPEC-073 Data-Source Ingestion & Routing (arcmemory)

**Feature:** `SPEC-073-arcmemory-datasource-ingestion`
**Created:** 2026-08-21
**Builds on:** connector-extensions ([2026-08-04](../../brainstorms/2026-08-04-connector-extensions.md), auth/transport), shipped SPEC-041/071/072 memory, SPEC-035 approvals.

## Status

| Doc | Status | Last Update |
|---|---|---|
| Brainstorm | complete | 2026-08-21 |
| Build decisions | complete (D-683..D-705 + Research Insights) | 2026-08-21 |
| PRD | approved | 2026-08-21 |
| SDD | approved | 2026-08-21 |
| PLAN | COMPLETE (33/33 tasks) | 2026-08-21 |

## Implementation Status: COMPLETE

All 33 tasks (T-1018..T-1050) implemented TDD, four phases, every task RED→GREEN verified.

- **Suite:** 597 passed, 1 skipped (pypdf extra absent), 1 xfailed (deferred postgres backend) in arcmemory; 56 arcagent memory-module tests green. `mypy --strict` clean (53 files); `ruff` clean; architecture tests green.
- **New tests added:** ~120 across unit/architecture (ingest API, IndexBackend conformance + vec0 scope isolation, config, router, memory sink, datastore, blob ontology, ingest security/provenance, extractor/chunker, doc index, sync, mapping approval, security envelope, triad E2E, boundary/determinism, degrade).
- **Two hardening gaps caught by the Phase-4 acceptance tests and fixed** (both `ingest.py`): (1) document-route chunks were stored unlabeled → a no-read-up leak on the doc axis (now threads `record.classification`); (2) `doc_search_enabled=False` gated only the read, not the ingest write (now gates both).

### Key implementation learnings
- **The un-scoped `vec0` scan was real, not theoretical.** `test_vec_scope_isolation` failed on the pre-existing global scan; the fix is a `JOIN chunks c ON c.chunk_id=v.chunk_id WHERE c.scope=?` inside the new `SqliteIndexBackend.vec_search`.
- **Async Protocol vs. sync test oracle.** `SurfaceIndex`'s `_bm25_search`/`_to_recall` are called synchronously by a locked test, so those stayed inline while the new `IndexBackend` methods are async — don't force a sync caller through an async Protocol.
- **`ApprovalStore` over a bare `open_backend('sqlite')` needs `backend.start()`** before first use (schema migration isn't implicit) — any non-test caller must start it too.
- **Optional-extra mypy overrides belong in ROOT `pyproject.toml`** (mypy reads one config from the invocation dir); a package-local `[[tool.mypy.overrides]]` is dead for root-run strict. Clear `.mypy_cache` after any `[tool.mypy]` edit — a stale cache reports a false green.
- **The doc index reuses `SurfaceIndex`'s SEARCH half over a dedicated `<did>:doc:<source>` scope** but upserts chunks directly (never `index_if_needed`, which would pull the agent's own memory files into the doc pool) — shared engine, separate pool.
- **Datastore introspection is dependency-free** (stdlib `sqlite3` PRAGMA); every table/column identifier is allowlisted against the introspected schema before it touches SQL, all values bound — SQLAlchemy stays a future opt-in for non-sqlite.

## Summary

arcmemory becomes the **routing brain** for connected data. On connect, the agent proposes a source→home mapping; the operator approves it once (SPEC-035 signed grant); the connection ingests and stays live. Each record is routed to one-or-more of three homes:

- **Memory** — events, outcomes, implicit procedures (reuses the shipped capture/consolidate path).
- **Document search** — blob files are extracted (narrow, sandboxed per-type extractors — never an all-in-one converter), chunked, embedded, and indexed into a **per-source hybrid index** (vec + BM25 + graph + recency, RRF-fused, optional rerank); arcmemory stores **chunk + pointer, never the bytes**.
- **Structured datastore** — a connected DB's schema is introspected into a bounded set of **typed, parameterized, read-only lookup ops** (`get_record`/`find`/`list`); "what is invoice 001" is an exact query, never a fuzzy match.

The agent retrieves through three distinct, source-scoped tools (`memory_recall`, `document_search`, `datastore_query`) registered in arcagent; arcrun only invokes them. The document index sits behind a pluggable `IndexBackend` (SQLite+sqlite-vec default, Postgres+pgvector opt-in) that mirrors `arcstore.backends`. Every ingest and retrieval is identity-scoped, classification-gated (no-read-up, per-provenance), bounded, audited, signed, and degrades rather than crashes.

**First proof — a triad, one connector per axis:** Slack (memory), Dropbox (document search), a SQLite database (datastore), each proven end-to-end on the real path with only the connector wire faked.

## Scope & Sequencing

23 requirements (REQ-363..385) → 15 components (COMP-001..015) → 33 tasks (T-1018..T-1050), four phases. Foundation lands the ingestion seam + the load-bearing `IndexBackend` refactor (extracting inline SQL out of `surface.py`, fixing the un-scoped `vec0` scan). Core builds the three axes. Integration wires mapping-approval, living-sync, the retrieval tools, and the security envelope. Polish proves the triad end-to-end and locks the boundaries. Regime `fedramp/nist`: identity + policy + audit auto-apply on every op; the SDD maps 8 OWASP LLM/ASI threats to mitigations.

## Steering References

- Product: [`../../steering/product.md`](../../steering/product.md) · Tech: [`../../steering/tech.md`](../../steering/tech.md) · Structure: [`../../steering/structure.md`](../../steering/structure.md) · Roadmap: [`../../steering/roadmap.md`](../../steering/roadmap.md)

## Decision Log

Build decisions `D-683..D-705` + `### Research Insights` in [`../../decisions-log.md`](../../decisions-log.md). Every COMP in the SDD maps to one or more D-NNN.

## Learnings

- **Un-scoped `vec0` is a pre-existing perf trap** (research): today's vector search scans all vectors globally; the seam refactor + scope filter is load-bearing so document chunks don't slow every agent's memory recall.
- **All-in-one extractors are an RCE surface** (LLM03/ASI05): narrow, sandboxed, per-type extractors are mandatory.
- **Both ontologies are Entity+Fact projections** on the existing `SemanticStore` — no new engine; the mapping's `was:` trail gives revision-on-shape-change for free.
- **Mirror `arcstore.backends`** for the `IndexBackend` seam so a future arcstore-wide Postgres move is a swap, not a reinvention.
- Related memory: [[project_arcmemory_architecture]], [[project_arcmemory_best_in_class_direction]], [[project_mechanical_approval_subsystem]], [[project_connectors_vendor_cli_rule]].

## Open Questions

- Federation-to-native-search classification-passthrough (`FederatedRecallAdapter`) — deferred fast-follow.
- A cheap "where should I look?" router hint vs the agent's own tool-selection.
- Per-source backfill-cap overrides surfaced in the approval artifact.

## Next Steps

- `/validate SPEC-073` — 3 Cs quality check on the spec.
- `/implement SPEC-073` — execute the plan (TDD, phase gates) on this branch.
