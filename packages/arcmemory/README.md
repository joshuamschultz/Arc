# arcmemory

Arc's memory substrate — a standalone package implementing **dual-speed
analogical memory** (SPEC-041). It replaces the two half-wired memory backends
that used to live inside `arcagent` with one clean system:

- **Fast path** (zero LLM, constant cost) — sanitize, privacy-filter, dedup, then
  append a raw episodic event + a daily-log bullet, tag entities deterministically,
  and apply a saturating Hebbian bump to co-active graph edges.
- **Four typed stores** — `episodic` (events), `semantic` (entities as fact-triplet
  graphs), `procedural` (how-to cards), and the centerpiece `insight`
  (patterns/theses with an abstracted trigger + cue tags).
- **Glass-box brain** — curated knowledge is human-editable markdown on disk (the
  source of truth); the raw stream and all derived indices (FTS5, `sqlite-vec`
  vectors, cue-graph edges/weights) live in arcmemory's own **per-agent SQLite**
  (`workspace/memory/index.db`), fully rebuildable from the files + stream.

## Concern boundary

`arcmemory` owns all memory mechanics. It **calls** `arcllm` (embed + distill) the
way `arcrun` does, persists to its own per-agent SQLite, audits through `arctrust`,
and is authorized via `arctrust` classification gating. It **never** imports
`arcagent` or `arcrun` and never runs the agent loop — `arcagent` talks to it only
through the `Brain` Protocol via hooks + a scheduled task.

DAG: `arcmemory → {arctrust, arcllm, arcstore}`, sibling to `arcrun`/`arcskill`,
below `arcagent`.

## Daily notes (glass-box daily-log)

The fast path writes a human-readable **daily note** per day at
`workspace/memory/daily-log/YYYY-MM-DD.md` — one bullet per captured turn
(`- <ts> [<kind>] <text>`), classification-gated in the file frontmatter. This is the
running "what was talked about, decisions, key entities, tasks" journal for each day, and it
is **distinct from `workspace/policy.md`**: policy.md is the self-improving system-prompt
playbook (the agentic-context-management bullets the policy module curates), whereas the
daily-log is plain context history you can read or edit by hand. Both entity cards
(`workspace/memory/entities/*.md`) and the daily-log are the glass-box source of truth; the
SQLite index is a rebuildable derivative.

## Install

```bash
pip install arcmemory            # core
pip install "arcmemory[vec]"     # + sqlite-vec surface/structural vector index
pip install "arcmemory[local]"   # + offline local embedder (bge-small / MiniLM)
```

arcmemory ships in the full-stack install (`pip install arcmas`) and the workspace dev env,
and **scaffolded agents default to `brain = "arcmemory"`** (`arc agent create` / `arc init` /
all blueprints) — so memory, the daily-log, and the episodic index are populated out of the
box. Set `[modules.memory].config.brain = "none"` for a memory-less agent.

## Status

All SPEC-041 phases have landed and are live as of **0.6.0**: the zero-LLM
deterministic capture path, surface + structural/analogical retrieval, slow-path
consolidation (distill facts/insights, promote procedures, decay unreinforced edges,
merge near-duplicate cues), the classification-gated no-read-up recall path, and the
`Brain` plug-in for `arcagent` with the arcllm-backed embedder + distiller seams
wired async-safe — so semantic vector recall and the analogical trigger channel run
in production. With neither seam injected the Brain still runs: capture stays
zero-LLM, recall degrades to BM25 + graph, and consolidation is a no-op.
