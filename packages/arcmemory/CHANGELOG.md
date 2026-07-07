# Changelog — arcmemory

All notable changes to the `arcmemory` package are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this package adheres to semantic
versioning.

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
