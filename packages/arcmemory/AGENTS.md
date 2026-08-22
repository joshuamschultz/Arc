# arcmemory

> **Build standards:** repo root [`AGENTS.md`](../../AGENTS.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Dual-speed analogical memory: glass-box markdown as source of truth, SQLite index, zero-LLM fast capture, structural + surface recall, agentic consolidation ("sleep"). SPEC-041.

## Layer

Sits **below** `arcagent`. Depends on `arctrust`, `arcllm`, `arcprompt`, `arcstore`, `arcrun`. **Never import `arcagent`** (`tests/architecture/test_no_arcagent_import.py`). `arcrun` only via `react_adapter.py`. Selected through `arcagent.brain.select_brain` / optional extra; default agent brain is `NullBrain`.

## Layout

```
src/arcmemory/
  brain.py              # ArcMemoryBrain (the Brain port)
  provider.py           # build_brain — arcagent's generic seam entrypoint
  capture.py            # FastCapture (zero-LLM fast path)
  retrieve.py           # Retriever (single-pass, gated recall)
  consolidate.py        # Consolidator (deterministic pipeline "sleep")
  agent_consolidate.py  # Agentic "sleep" — bounded ReAct loop (default engine)
  tools.py              # Signed/authorized/audited memory-tool registry
  react_adapter.py      # Sole arcrun touchpoint
  distill.py            # Distiller seam (facts / insights / procedures / dedup)
  arcllm_seam.py        # ArcLLMEmbedder / ArcLLMDistiller (arcllm-backed seams)
  hygiene.py            # Nightly dedup / backlink repair / alias merge
  curate.py             # Deterministic conversation-only input filter
  operator.py           # MemoryOperator — typed read/search/mutate facade
  stores/               # episodic / semantic / procedural / insight / events / daily
  index/                # graph / surface / structural / rebuild / source
  db.py                 # MemoryDB — per-agent SQLite (self-migrating)
  security.py           # ACL / no-read-up classification gating
  acl.py                # Session ACL / cross-session visibility
  degrade.py, status.py # Loud semantic-degrade + operator status readout
  fusion.py, tagging.py, mdfile.py, slug.py, types.py, config.py
  context/              # Stock prompts (distill / consolidate)
```

## Entry points

`ArcMemoryBrain`, `build_brain`, store types, `FastCapture`, `Retriever`, `Consolidator`, `run_agentic_consolidation` / `build_memory_tools`, `MemoryOperator`, indexes, `ArcLLMEmbedder`/`ArcLLMDistiller`, hygiene helpers, security helpers, `semantic_status` / `semantic_degraded`.

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