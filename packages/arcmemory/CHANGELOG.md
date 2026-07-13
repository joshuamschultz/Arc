# Changelog — arcmemory

All notable changes to the `arcmemory` package are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this package adheres to semantic
versioning.

## [Unreleased]

### Added

- **`MemoryOperator`** (`arcmemory.operator`, T-702/703, COMP-001) — the single typed
  read/search/mutation facade arcui (and any other consumer) uses instead of touching SQLite
  directly (REQ-087). Paged episodic listing with created/recency/importance(1–10)/source
  metadata, entity listing + graph link traversal, ranked search delegating to the production
  `Retriever`, and honest mutations (`edit_entry`, `set_metadata`, `delete_entry` →
  `MutationResult` applied|error, never partial) accepting an actor DID for the audit trail.

### Fixed

- **CRITICAL — the episodic table's `salience`/`entities` columns were missing on every
  pre-existing database.** Both were added to `episodic`'s `CREATE TABLE IF NOT EXISTS` — a
  no-op against a table that already existed on every deployed agent, so every capture, recall,
  and facade call threw `OperationalError: no such column: salience` (memory silently down
  fleet-wide; fresh agents were unaffected). `MemoryDB` now runs a generalized
  `_ensure_columns` self-migration (`PRAGMA table_info` → `ALTER TABLE ADD COLUMN`) idempotently
  at `connect()` — the seam so the *next* added column doesn't repeat this, not a migration
  framework. Verified with a fixture hand-written from the exact pre-migration schema (git
  history, not inferred): zero data loss, capture and the `MemoryOperator` facade both work
  post-migration.

### Changed

- **`sqlite-vec` is now a base dependency** (was the optional `[vec]` extra). Semantic
  vector recall (surface + structural channels) works **out of the box** — no
  `arcmemory[vec]` install step. Load-time guarding is unchanged: if the extension
  is somehow unavailable, retrieval still degrades to BM25 + graph and never fails.
  `arcmemory[vec]` is retained as a no-op alias. *(Live-test follow-up — Josh: "full use out of the box".)*

## [0.6.0] — 2026-07-07

The embedder + distiller seams go **live in production** (SPEC-041, Phases 10/11).
Phases 8/9 shipped the brain with both seams set to `None`, so semantic vector
recall, the analogical *trigger* channel, and consolidation insight-minting were
dark. This release wires them to arcllm — async-safe — and adds the end-to-end
integration ACs that drive the real path.

### Added

- **`arcllm_seam.ArcLLMEmbedder` / `ArcLLMDistiller`** — the arcllm-backed adapters
  for the `Embedder` and `Distiller` seams. `ArcLLMEmbedder.embed_texts` awaits
  `arcllm.embed`; `ArcLLMDistiller` runs two bounded structured completions (fact
  extraction + insight minting) through a per-call `provider_factory` (a fresh,
  self-closing provider — consolidation is off the hot path). Both live in arcmemory
  (which depends on arcllm), so arcagent's `select_brain` can light them up without
  arcagent holding memory logic.
- **`index.rebuild.EmbeddingUnavailableError` + `embed_or_none`** — the single degrade
  funnel: a never-wired embedder (`None`) and a wired-but-unavailable one (the arcllm
  `[local]` extra absent) both collapse to a `None` vector channel → BM25 + graph,
  audited, never a crash (REQ-041).
- **Phase-10 wired-brain integration suite** (`tests/integration/test_wired_brain.py`)
  — the ACs driven through `ArcMemoryBrain` with the seams wired: zero-LLM capture
  (AC-2), consolidate mints/promotes/decays via the wired distiller (AC-4), semantic
  no-lexical-overlap recall on real vectors (AC-5), the **structural probe retrieved
  via BOTH channels only through the wired embedder** (AC-6, the differentiator),
  no-read-up + fail-closed + injection-inert recall (AC-8), byte-identical rebuild and
  embedder-disabled degrade (AC-3/AC-7). Plus adapter unit tests (`test_arcllm_seam`).

### Changed

- **The `Embedder` seam is now async** (`async def embed_texts`). Embedding happens
  inside the already-async `retrieve()` / `consolidate()` / `index_if_needed()` /
  `trigger_index()` / `rebuild()` paths, which now `await` the seam directly on the
  running event loop — **no nested loop, no `run_until_complete`, no thread that
  blocks the loop**. This closes the Phase-8 sync/async hazard that left the seams
  unwired. `Retriever.index`, `SurfaceIndex.{index_if_needed,search}`,
  `StructuralIndex.{trigger_index,trigger_match}`, `Consolidator.{merge_cues,recover}`,
  `IndexRebuilder.rebuild`, and `ArcMemoryBrain.rebuild_index` are async accordingly.

## [0.5.0] — 2026-07-07

The `Brain` plug-in for arcagent (SPEC-041, Phase 8). arcagent defines a structural
`Brain` Protocol + no-op `NullBrain` and depends on no memory package; this release
adds the concrete implementation that satisfies it.

### Added

- **`brain.ArcMemoryBrain`** — the concrete Brain that wraps the three memory speeds
  behind arcagent's structural Protocol: `capture` → `FastCapture` (zero-LLM),
  `retrieve` → `Retriever` (single-pass, clearance-gated, boundary-marked, returns
  injectable text), `consolidate` → `Consolidator` (slow sleep path; returns mutation
  counts + an `episode_summary` for grounded reflection), and `rebuild_index`. Bound
  to one `agent_did` + workspace; a per-call `session_id` narrows the shared-nothing
  scope. The embedder/distiller are injected seams — with neither present capture is
  still zero-LLM, recall degrades to BM25 + graph, and consolidation is a no-op, so
  the Brain never errors on a bare install. Speaks only primitives at its edge and
  imports nothing from arcagent (the architecture boundary test stays green).

## [0.4.0] — 2026-07-07

Retrieve orchestration + classification-gated recall (SPEC-041, Phase 7). The single
bounded read path that fuses both channels, enforces no-read-up, and hands back a
boundary-marked, budget-capped bundle safe to inject.

### Added

- **`retrieve.Retriever`** (T-070) — the one bounded read path (`retrieve.py`). A
  single pass (no agentic loop, LLM10): fuse the surface channel (Phase 4) and the
  structural channel (Phase 6) with **RRF** (`1/(k+rank)`, k=60), confidence-gate
  (`guessed` → "verify first", `known` → actionable), then apply the no-read-up gate,
  then bound to top-k + token budget. Returns a `Bundle` whose `text` is the
  injectable, boundary-marked rendering.
- **No-read-up gate** `security.gate_no_read_up` (T-070/071) — **reuses**
  `arctrust.dominates` / `arctrust.parse_classification`; arcmemory defines **no
  comparator of its own** (enforced by `tests/architecture/test_reuses_arctrust_comparator.py`
  — runtime identity + static AST scan). Each memory's `classification` label is
  mapped onto the arctrust ladder and dropped when the caller's clearance does not
  dominate it (Bell-LaPadula, NIST 800-53 AC-4). A `SECRET` memory is dropped for an
  `UNCLASSIFIED` caller; a `CUI` memory is kept for a `SECRET` caller.
- **Fail-closed labeling** (T-071) — federal (`strict`) **rejects** an unlabeled or
  unknown-labeled memory; personal warns and defaults `UNCLASSIFIED` (ADR-019). Every
  drop emits a `recall.dropped` audit event (AU-2) carrying only a content **hash** —
  never plaintext. The gate filters **before assembly**, so a dropped memory leaks
  nothing via the returned bundle's rank, count, `text`, or any error.
- **Boundary marking + budget** `security.boundary_mark` / `render_recalls` /
  `enforce_budget` (T-072) — each injected memory is wrapped in a `<memory-result>`
  block carrying source + score + confidence, framed as untrusted **DATA, never
  instructions** (LLM01). Forged boundary markers inside stored text are defanged, so
  a prompt-injection-laden memory cannot break out of its own block — rendered inert.
  Top-k + token budget are enforced by truncating the **lowest-ranked first**, never
  overflowing.
- `Bundle.text` — the injectable boundary-marked rendering of the kept recalls.

### Changed

- `index/surface.py` — a source file with a **genuinely missing** `classification`
  frontmatter now passes an empty label through to the gate (was silently defaulted
  to `unclassified`). The index no longer makes the security decision; the no-read-up
  gate does — so federal fail-closed actually triggers on an unlabeled file (SDD §8).

## [0.3.0] — 2026-07-07

Structural / analogical retrieval — the centerpiece (SPEC-041, Phase 6). This is the
differentiator: retrieving a recurring pattern/thesis whose match to the present is
**structural, not lexical** — zero surface overlap with the original episodes.

### Added

- **Trigger index** `index/structural.py::trigger_index` (T-060) — embeds each
  insight `trigger` into a **separate** `insight_trigger` table, kept apart from the
  surface `vec0` chunks so surface noise cannot drown a minted abstraction. Content-
  gated (only new/changed triggers re-embed, riding the SPEC-038 budget). A plain
  float32-blob table, so the trigger channel works without `sqlite-vec` and degrades
  cleanly (to the cue-graph channel only) when no embedder is injected.
- **Channel (a) trigger-embedding** `structural.trigger_match` (T-061) — abstracts
  the current situation (default: reuse the turn's existing summary — no new LLM
  call, OQ-1), embeds it, and cosine-matches insight *trigger* vectors. A situation
  described at the *mechanism* level matches an insight whose trigger shares **no
  surface token** with the original episodes.
- **Channel (b) cue-graph spreading activation** `structural.cue_match` (T-062) —
  lights the abstract cue nodes the situation implies and flows activation over the
  **existing** `WeightedGraph.spreading_activation` (ACT-R base-level, fan effect,
  hop-capped, zero-LLM) to the insight nodes whose cues are active — retrieving with
  **zero lexical/semantic overlap** to the instances. The graph edges *are* the
  learned "situation-shape → pattern" map.
- **Confidence gate** (T-063) — `known` insights are actionable anchors; `guessed`
  insights are surfaced tentatively (`verify_first`). Conjunctive gating (both
  channels must agree — R-8 false-positive control) means a never-recurring `guessed`
  insight, whose cue edges decay below the forget floor over time, silently **decays
  out** of retrieval.
- **Enrichment** `structural.enrich` (T-064) — "spot, then enrich": from a matched
  insight, traverse to its instance episodes, the entities they mention, the adjacent
  insights sharing its cues (bounded hops), and the surrounding raw-stream events
  (`enrich_stream_radius`).
- **Optional cross-encoder rerank** `structural.match(reranker=…)` (T-065) — an
  injected `Reranker` seam over the *small* structural candidate set, tier-gated:
  personal OFF (with a deterministic top-1/top-2 `rerank_margin` fallback),
  enterprise/federal ON. The reranker verdict only reorders; it never enters agent
  context (LLM09/LLM10, bounded, off the hot path).
- **AC-6 planted structural probe** (the acceptance proof) — past episodes are
  consolidated into an insight with a surface-stripped trigger + abstract cues, then
  a **new situation in a different domain with mechanically-asserted ZERO surface
  overlap** is retrieved via **both** channels and enriched with its instances; a
  never-recurring `guessed` insight decays out over simulated time.

### Changed

- `db.py` — added the separate `insight_trigger` table (+ scope index) to the
  per-agent schema; `insight_trigger` joins `DERIVED_TABLES` and `wipe_derived`
  (disposable, re-derived by `trigger_index`).
- `config.py` — added `struct_trigger_min`, `struct_activation_min`, `rerank_margin`,
  and `enrich_stream_radius` tuning constants.

## [0.2.0] — 2026-07-07

Surface retrieval + slow-path consolidation (SPEC-041, Phases 4/5).

### Added

- **Surface retrieval** `index/surface.py` (T-040/041/042) — the easy channel.
  `index_if_needed()` is incremental and content-gated: only new/changed chunks are
  embedded (via the injected `Embedder` seam over `arcllm.embed`), so re-indexing an
  unchanged workspace is free. `search()` fuses three ranked lists — vec cosine
  (brute-force over `vec0`), BM25 (FTS5), and a graph spreading-activation signal —
  with Reciprocal Rank Fusion (k=60), plus **recency as a fourth ranked list** (not
  a score multiplier, preserving RRF's scale-free property). A no-lexical-overlap
  but semantically-related query retrieves the right chunk (embeddings, not
  substrings), and fusion beats BM25-alone. **Degrades** to BM25 + graph with a
  `recall.degraded` audit signal (never raises) when `sqlite-vec` or the embedder
  is unavailable.
- **Distillation** `distill.py` (T-050/051) — the one LLM path, an injected
  `Distiller` structured-completion seam (deterministic-testable; production wires
  `arcllm`, tests inject a fake). `extract_facts` applies facts **additively** —
  a contradiction folds the prior into a `was:` trail rather than overwriting — with
  confidence `1 − e^(−γ·hits)` that rises with corroboration. `mint_insights` mints
  abstraction cards `{statement, trigger, cues[], instances[]}`, `guessed` on first
  mint, wiring each cue as a graph node. A cluster of structurally-similar but
  lexically-different episodes mints one insight whose trigger is surface-stripped.
- **Consolidation** `consolidate.py` (T-052/053/054) — the "sleep" orchestrator.
  `run(window)` extracts facts, mints insights, promotes repeated action-sequences
  to procedures (`ProceduralStore.promote`, zero-LLM), decays unreinforced edges
  (salience-slowed — a rare-but-vital edge survives while neutral noise fades),
  merges near-duplicate cues (`merge_cues`, embedding-cluster, repointing links via
  `WeightedGraph.rename_node`), and reindexes touched chunks. Every mutation emits an
  `AuditEvent` (fact / insight / procedure / decay / file-write / cue-merge); the
  chain verifies. A write-ahead `.consolidate-manifest.json` makes an interrupted run
  crash-safe: `recover()` rebuilds the disposable index from the curated files and
  clears the marker.
- **`TimeWindow`** type — the bounded slice of the raw stream one run reads.

## [0.1.0] — 2026-07-07

Foundation of Arc's dual-speed analogical memory substrate (SPEC-041, Phases 0/2/3).

### Added

- **Package scaffold** (T-001/T-002/T-003) — installable `arcmemory` (src layout,
  `py.typed`, own tests), depending only on `arctrust` / `arcllm` / `arcstore`
  (never `arcagent` / `arcrun`, enforced by an import-graph architecture test).
  Extras `[vec]` = sqlite-vec and `[local]` = sentence-transformers. Registered in
  the uv workspace + CI matrix.
- **Typed models** — `Event`, `Fact`, `Entity`, `Procedure`, `Insight`, `Cue`,
  `Recall`, `Bundle`, `Scope`, `Situation`, `ConsolidationResult` (Pydantic v2,
  mypy-strict, round-trip serializable).
- **Per-agent SQLite substrate** (T-020) — `MemoryDB` owns
  `<workspace>/memory/index.db` with `episodic`, `chunks`, `fts_chunks` (FTS5),
  `edges`, and a guarded `vec0` (sqlite-vec) table. One file per agent workspace =
  hard shared-nothing isolation. Everything derived is disposable/rebuildable.
- **Four stores** (T-021/T-022/T-023) — `episodic` (raw stream + daily-log
  bullets), `semantic` (entity markdown + fact-triplet graph, additive `was:`
  contradiction trail), `procedural` (how-to cards with use-count), `insight`
  (pattern cards: statement/trigger/cues/instances/confidence/salience).
- **Weighted graph** (T-024) — `WeightedGraph` with saturating Hebbian bump
  (`w <- w + a*m*(1 - w/W)`), salience-slowed decay (`w*e^(-lambda_eff*dt)`,
  `lambda_eff = lambda*(1 - beta*s)`), and hop-capped ACT-R spreading activation.
- **Index rebuild** (T-025) — `IndexRebuilder` re-derives `fts_chunks` + `vec0` +
  `edges` from the glass-box files + raw stream; wipe→rebuild is byte-identical.
  The vector step uses an injected `Embedder` seam (arcllm-backed in production,
  stubbed in tests).
- **Zero-LLM fast capture** (T-030/T-031/T-032) — `security.sanitize` /
  `privacy_filter` / `Deduper` (absorbed sanitizer), and `FastCapture`:
  sanitize → filter → dedup → episodic + bullet → deterministic entity tag →
  Hebbian bump, with **no LLM/embedding call** (spy-verified) at constant cost,
  emitting a `memory.captured` audit event.

### Absorbed (from the old arcagent memory backends; originals removed in Phase 8)

- `bio_memory/facts.py` — fact-triplet grammar (`predicate: value .conf date | was:`).
- `bio_memory/entity_helpers.py` — entity files, frontmatter, wiki-links.
- `memory/hybrid_search.py` — FTS5 skeleton (now backed by a real `vec0` layer).
- `utils/sanitizer.py` — `sanitize_text` and invisible/injection stripping.
- daily-notes / `working.md` behavior — episodic stream + daily-log bullets.
