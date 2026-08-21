# Product Requirements Document: Data-Source Ingestion & Routing (arcmemory)

## Context References

- **Brainstorm:** [2026-08-21-arcmemory-datasource-ingestion](../../brainstorms/2026-08-21-arcmemory-datasource-ingestion.md)
- **Build decisions:** [decisions-log D-683..D-705 + Research Insights](../../decisions-log.md)
- **Personas / constraints:** [.claude/steering/product.md](../../steering/product.md)
- **Tech / compliance (fedramp/nist):** [.claude/steering/tech.md](../../steering/tech.md)
- **Builds on:** connector-extensions ([2026-08-04](../../brainstorms/2026-08-04-connector-extensions.md), auth/transport) and shipped SPEC-041/071/072 memory.

## Product Overview

### Vision
arcmemory becomes the **routing brain** for connected data. On connect, the agent maps a source into the right home — **memory** (events/outcomes/procedures), **document search** over blob storage (an ontology + hybrid index so files are findable without copying them in), or a **structured datastore** (an ontology for exact lookup) — the operator approves once, and the connection stays live. The agent then retrieves like a specialist through three purpose-built tools.

### Problem Statement
The connector work gives an agent hands, but connected data lands nowhere useful: a connected source is inert until its content is routed to a home and retrievable in a turn. Not everything belongs in memory — a five-year S3 bucket must be searchable in place, and an invoice is an exact lookup, not a similarity match. arcmemory already owns capture/consolidation/recall, so it is the natural place to decide where connected content lives and how it comes back — but it has no ingestion seam, no document-search axis, and no datastore axis today.

### Value Proposition
A connected source stops being inert and becomes usable — routed to the right home, retrievable with the right tool, and kept fresh — while every ingest and retrieval stays deterministic on the trigger path, classification-gated (no-read-up), bounded, audited, and behind a config off-switch. Blobs and rows stay in place; arcmemory holds ontology + index + pointers, never the bytes.

## Personas

See `.claude/steering/product.md#user-personas`.
- **Operator (Josh, primary):** connects his Slack, files, and data; approves the mapping once; expects it usable and self-updating.
- **Agent Developer (internal):** wants routing + retrieval behind the Brain port so arcmemory owns the logic and arcagent only registers tools.
- **Federal/Regulated Security Architect:** needs every ingest and retrieval identified, classification-gated, bounded, signed, and audited — a new document/datastore channel must not weaken this.
- **Scale/SaaS operator (later):** needs the index backend to move SQLite→Postgres without a rewrite.

## User Stories

- **US-1** — As an operator, I connect a source, the agent proposes what becomes memory vs doc-search vs database, I approve once, and it ingests and stays fresh — so my agent knows and uses my data without an engineering project.
- **US-2** — As an agent, I route each record to the right home and retrieve with the right tool (analogical memory, hybrid document search, exact database lookup) — so I act on the full picture with clean provenance.
- **US-3** — As a federal architect, every ingested item and every retrieval result is classification-gated, bounded, audited, signed, and defanged against injection — so more reach never costs safety.
- **US-4** — As a scale operator, the document index moves from SQLite to Postgres+pgvector by config, not rewrite — so a personal deploy stays zero-config and a SaaS deploy scales.

## Functional Requirements

Ingestion & routing:
- **REQ-363** (US-1, Must): WHEN a connected source delivers content THEN arcmemory SHALL ingest it via a Brain-port push seam (`register_source` / `propose_mapping` / `ingest_batch`) where the connector owns a resumable fetch cursor and arcmemory ingests idempotently by a deterministic `event_id = sha256(source_id + external_id)` (Simplicity: mirrors the existing capture/on_moment ports) [D-684].
- **REQ-364** (US-1, Must): WHEN a record is ingested THEN a single Router SHALL dispatch it to one-or-more homes (memory sink / doc-search indexer / datastore registrar) per the approved mapping, via thin per-source adapters (Modularity: one routing brain, pluggable adapters) [D-683].
- **REQ-365** (US-1, Must): WHEN a source is connected THEN the agent SHALL propose a source→home mapping and the operator SHALL approve it once via the SPEC-035 signed operator-DID grant before ingest commits (Security: a signed, out-of-band, audited routing decision) [D-691].

Memory axis:
- **REQ-366** (US-2, Must): WHERE the mapping routes a record to memory arcmemory SHALL ingest it through the existing capture + consolidation path (events, outcomes, implicit procedures) (Simplicity: reuse the shipped memory path) [D-688].

Document-search axis:
- **REQ-367** (US-2, Must): WHERE the mapping routes a source to document-search arcmemory SHALL extract text with a per-type safe extractor run out-of-process and resource-capped, chunk it (recursive 512-token / 10% overlap; AST chunking for code), embed, and index into a per-source hybrid index — storing chunk + pointer, never the raw bytes (Security: no all-in-one converter RCE surface; Simplicity: reuse the SurfaceIndex engine) [D-685, D-686].
- **REQ-368** (US-2, Must): WHEN `document_search` is called THEN arcmemory SHALL return hybrid results (vector + BM25 + graph + recency, RRF-fused, optionally reranked on the bounded top-K) scoped to a source/container, each carrying provenance (home + source + pointer) (Simplicity: reuse rrf_fuse + the tier-gated Reranker) [D-690].
- **REQ-369** (US-4, Must): WHILE document chunks are indexed arcmemory SHALL keep them in their own per-source scope so vector search is scope-filtered and doc chunks never flood or slow memory recall (Scalability: isolated pools; fixes the un-scoped `vec0` scan) [D-686].

Datastore axis:
- **REQ-370** (US-2, Must): WHERE the mapping routes a source to the datastore home arcmemory SHALL introspect its schema and generate a bounded set of typed, parameterized, read-only lookup operations (`get_record` / `find` / `list`), row- and time-capped, on a read-only connection — never agent-authored or generated raw SQL (Security: zero injection surface, LLM05) [D-689].
- **REQ-371** (US-2, Must): WHEN `datastore_query` is called THEN it SHALL expose only those typed operations, following at most a one-hop foreign-key relationship (Simplicity: exact lookup, not a SQL console) [D-689].

Ontology:
- **REQ-372** (US-1, Should): WHEN a source is mapped THEN arcmemory SHALL represent its ontology as Entity+Fact projections (a coarse `blob_folder`/`blob_container` catalog for files; a `db_table` schema map for datastores) in glass-box markdown plus a disposable index, and SHALL store the mapping itself as facts so the additive `was:` trail carries a re-proposal when the source changes shape (Simplicity: reuse SemanticStore, no new engine) [research; D-691].

Living connections:
- **REQ-373** (US-1, Must): WHILE a source is connected arcmemory SHALL keep it fresh — immutable sources (email, past messages) ingest append-only (backfill once, cursor forward), mutable sources (files, pages) reconcile with upsert/delete tombstones (never a hard delete; AU-2) (Scalability: bounded, mutability-driven sync) [D-688].
- **REQ-374** (US-1, Should): WHERE incremental sync polls arcmemory SHALL reuse arcagent's scheduler via a signed `workflow_run` trigger carrying no prose, with webhook receipt owned connector-side — arcmemory SHALL NOT import the scheduler or connector packages (Modularity: Brain port + primitives only) [D-703].
- **REQ-375** (US-4, Must): WHEN a source is backfilled arcmemory SHALL enforce windowed/size caps (default 90-day window, 10MB-per-object for embedding, 5GB-or-50k-object hard stop with an audit + resumable status) BEFORE the embed step and re-validate the cap at the `ingest_batch` boundary (Scalability: bounded consumption, LLM10) [D-704].

Backend:
- **REQ-376** (US-4, Must): The document index SHALL sit behind a pluggable `IndexBackend` Protocol with a factory whose deferred backends raise explicitly (no silent fallback); SQLite+sqlite-vec is the zero-config default and Postgres+pgvector an opt-in extra, requiring the inline vector/FTS SQL to be extracted out of `surface.py` into a `SqliteIndexBackend` (Modularity: mirror the `arcstore.backends` pattern) [D-687, D-702].
- **REQ-377** (US-4, Should): WHEN the backend is swapped THEN the RRF fusion, graph, recency, degrade, and no-read-up gate SHALL require no change (they operate above the Protocol on `(id, score)` lists) (Modularity: the seam is drawn correctly) [research].

Retrieval surface:
- **REQ-378** (US-2, Must): The agent SHALL retrieve through three distinct, source-scoped tools — `memory_recall` (exists), `document_search`, `datastore_query` — registered in arcagent's registry (logic in arcmemory, arcrun only invokes), each returning typed, provenance-carrying results (Simplicity: distinct tools, legible provenance) [D-690].

Security (cross-cutting, fedramp/nist):
- **REQ-379** (US-3, Must): WHEN an item is ingested or a result retrieved THEN arcmemory SHALL assign a classification by precedence (source-native → operator-set container → fail-closed default) and gate every home no-read-up; an optional content scan MAY only RAISE a label, never lower it (Security: AC-4, fail-closed at federal) [D-695; research].
- **REQ-380** (US-3, Must): WHEN ingested content reaches the model THEN it SHALL be sanitized and boundary-marked as inert DATA via a document-aware `document_sanitize` (chunked, stronger prose patterns, an audit event on every injection-pattern match), never placed in a system-role position, and extractors SHALL run sandboxed and capped (Security: LLM01/ASI06 defense-in-depth; treat injection as risk-reduction) [D-696, D-698; research].
- **REQ-381** (US-3, Must): Every ingest batch, mapping proposal/approval, and retrieval/query SHALL carry `caller_did`, pass the 5-layer PolicyPipeline (fail-closed), and emit an `AuditEvent` through the single `arctrust.audit.emit` chokepoint (Security: NIST IA/AU/AC) [D-692, D-693, D-694].
- **REQ-382** (US-3, Should): WHEN the same artifact arrives from more than one source THEN arcmemory SHALL keep one canonical item keyed by content hash with an additive provenance list, and SHALL gate retrieval against the specific provenance surfaced, not the item's dominating label (Security: no over-denial, no under-exposure) [research].

Degrade & config:
- **REQ-383** (US-3, Must): Every new path SHALL degrade rather than crash — no embedder falls back to BM25 + graph, a source offline serves what is already indexed, an unlabeled item fails closed — and each capability (doc-search, datastore, per-source sync) SHALL have a config off-switch that returns prior behavior (Simplicity + Security: safe by default) [D-700].
- **REQ-384** (US-3, Must): The first proof SHALL be a real-path end-to-end triad — Slack (memory), Dropbox (document-search), a SQLite database (datastore) — exercising connect→propose-map→approve→ingest→ontology→retrieve→keeps-updated with only the connector wire faked, each axis falsifiable by disabling its capability (Security: end-to-end-through-real-path, no producers-unwired trap) [D-701].
- **REQ-385** (US-3, Must): Connectors SHALL hold source credentials (vault-backed, never on the filesystem) and this layer SHALL never see a raw secret; source fetches use TLS; connectors are signed, verified extensions (Security: LLM07 / supply chain) [D-697, D-698].

## MoSCoW Priorities

| Priority | Requirements |
|---|---|
| Must | REQ-363, 364, 365, 366, 367, 368, 369, 370, 371, 373, 375, 376, 378, 379, 380, 381, 383, 384, 385 |
| Should | REQ-372, 374, 377, 382 |
| Could | _(none in v1)_ |
| Won't (v1) | Building/re-building the connectors; MCP transport; a unified auto-merging retriever; write-back to sources; federation to native search; UI beyond CLI + approval; sources beyond the eight; copying blob bodies or DB rows into memory; a general SQL console |

## Success Metrics

See `.claude/steering/product.md#success-metrics-framework`. Targets: the triad proves each axis end-to-end on the real path (only the connector faked); a Dropbox document is found by hybrid search and read on demand without its bytes stored in memory; "what is invoice 001" resolves as an exact typed query, never a fuzzy match; 0 classification leaks across doc-search and datastore results (security test); ingest is bounded (no backfill exceeds the caps); doc chunks add 0 measurable latency to memory recall (per-source scope isolation); the index backend swaps SQLite→Postgres with no change to fusion/gating code; 0 raw secrets reach this layer.

## Risks & Constraints

- **The connectors are separate work** (2026-08-04): this layer consumes them via the Brain port and must not rebuild transport/sync. Auth/webhook/cursor stay connector-side.
- **Un-scoped `vec0`** (research): today's vector search scans globally; the seam refactor + scope filter is load-bearing, not optional, or doc chunks slow every agent's recall.
- **Untrusted files** (LLM03/ASI05): all-in-one extractors (markitdown/Docling) are an RCE surface; narrow per-type extractors, sandboxed, are mandatory.
- **Prompt injection from documents** (LLM01): sanitize is risk-reduction, not prevention; ingested content never gains tool authority and never sits in a system-role position.
- **Bounded consumption** (LLM10): backfill caps enforced before embed.
- **Scope:** three axes in one spec — the datastore axis is the least precedented; the first-proof triad de-risks all three by proving each on the real path.

## Open Questions

- Federation-to-native-search (deferred fast-follow): the classification-passthrough contract a connector must satisfy before its native results are trusted.
- Whether the three tools need a cheap "where should I look?" router hint, or the agent's tool-selection suffices.
- Exact per-source backfill-cap overrides surfaced in the approval artifact.
