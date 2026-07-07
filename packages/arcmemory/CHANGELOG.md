# Changelog — arcmemory

All notable changes to the `arcmemory` package are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this package adheres to semantic
versioning.

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
