# arcmemory

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Dual-speed analogical memory: glass-box markdown as source of truth, SQLite index, zero-LLM fast capture, structural + surface recall, agentic consolidation (“sleep”). SPEC-041.

## Layer

Sits **below** `arcagent`. Depends on `arctrust`, `arcllm`, `arcprompt`, `arcstore`, `arcrun`. **Never import `arcagent`** (`tests/architecture/test_no_arcagent_import.py`). `arcrun` only via `react_adapter.py`. Selected through `arcagent.brain.select_brain` / optional extra; default agent brain is `NullBrain`.

## Layout

```
src/arcmemory/
  brain.py              # ArcMemoryBrain, build_brain
  capture.py            # FastCapture
  retrieve.py           # Retriever
  consolidate.py        # Consolidator
  stores/               # episodic / semantic / procedural / insight / events
  index/                # graph / surface / structural
  arcllm_seam.py        # Embedder / distiller seams
  react_adapter.py      # Sole arcrun touchpoint
  operator.py
  security.py           # ACL / classification gating
  context/              # Stock prompts
```

## Entry points

`ArcMemoryBrain`, `build_brain`, store types, `FastCapture`, `Retriever`, `Consolidator`, indexes, `ArcLLMEmbedder`/`ArcLLMDistiller`, security helpers, `semantic_status` / `semantic_degraded`.

## Package rules

- Implement the structural `Brain` Protocol — do not couple to `ArcAgent`.
- Markdown is source of truth; SQLite index is disposable/rebuildable.
- **Degrade, don't crash:** no embedder → BM25+graph; no LLM → deterministic consolidate.
- Classification: no-read-up gating on recall.
- Keep `arcrun` confined to `react_adapter.py`.

## Tests

`packages/arcmemory/tests/` — `unit/`, `integration/`, `security/`, `architecture/`, `performance/`. See also `SETUP.md`.

## Working here

Wire via `build_brain` and the optional `arcagent[memory]` extra. Prefer store/index changes over pulling agent orchestration downward.
