# Tune Knowledge and Memory

> **Get Started**  ·  Tune  ·  the knobs that shape recall and consolidation
> **For** operators who have a source connected and want to shape how the agent remembers and retrieves
> [Docs home](../README.md)  ·  [Memory, the index & scope (why) →](../concepts/memory-index-and-scope.md)  ·  [Memory lifecycle →](../walkthrough/07-memory-lifecycle.md)

---

## In one breath

Arc's memory is **dual-speed**: fast capture writes every turn to an episodic
stream, and a slower **consolidation** pass distills, dedups, and files durable
markdown. Retrieval fuses four channels — vector, BM25, graph, and recency —
into one bounded result, joined against per-scope chunks so one agent never
scores another's vectors. Most of what you tune falls into three groups:
**consolidation cadence** (how often the sleep pass runs), **retrieval and
chunking** (how documents are split and ranked), and **the index backend**
(SQLite by default, pgvector when you opt in). One subtlety worth getting right:
the coarse knobs live in the agent's `[modules.memory]` block; the finer
arcmemory tuning is passed through a `dynamics` sub-table.

## The two config layers

The agent-side memory **module** (`arcagent/modules/memory/config.py`,
`MemoryConfig`) holds the coarse switches. It forwards a `dynamics` table into
arcmemory's **own** `MemoryConfig` (`arcmemory/config.py`), which holds the
fine-grained tuning (`provider.py:59` builds
`MemoryConfig(**{**config.model_dump(), **dynamics})`).

```toml
# arcagent.toml — coarse module knobs
[modules.memory.config]
brain = "arcmemory"              # "none" disables memory
tier = "personal"
working_set_enabled = true
consolidate_interval_seconds = 3600.0    # min seconds between sleep passes
consolidate_event_threshold = 20         # events that trigger a pass
consolidate_idle_seconds = 900.0
embed_backend = ""               # set to enable semantic (vector) retrieval
embed_model = ""

# deeper arcmemory tuning goes here (validated by arcmemory's MemoryConfig)
[modules.memory.config.dynamics]
consolidate_interval_minutes = 60.0
working_set_max = 32
doc_chunk_tokens = 512
```

## Consolidation cadence

Consolidation is gated so a chatty agent can't burn tokens re-distilling nothing.

- **Light "sleep" pass** — `Consolidator.run`
  (`packages/arcmemory/src/arcmemory/consolidate.py:216`), gated by `due(now,
  interval_minutes)` (`:206`): *"an agent may ask to consolidate every turn, but
  it only actually runs once the interval passes."* The interval is
  `consolidate_interval_minutes` (default `60.0`, `arcmemory/config.py:108`).
- **Deep nightly hygiene** — `run_hygiene` (`consolidate.py:359`), gated by
  `hygiene_due()` (`:348`, first call of a new local day): runs the light pass,
  then deterministic entity-merge, back-link repair, and workspace dedup.
- **The interactivity gate (D-726).** Consolidation keys off
  `turn_context.interactive()`
  (`packages/arcagent/src/arcagent/core/turn_context.py:96`) — *"only real
  interaction counts toward 'there is new context to fold in', so a quiet agent's
  background churn cannot keep re-triggering an expensive consolidation on
  nothing new."* This is the fix behind the idle-runaway cost bug; leave it on.

The consolidation **engine** is agentic by default (a bounded ReAct loop):
`consolidate_engine="agentic"`, with `consolidate_agent_max_turns`,
`consolidate_agent_max_tokens`, and `consolidate_agent_timeout_seconds` caps
(`arcmemory/config.py:117–126`). Federal tier tightens these automatically
(`for_tier`, `:250`).

## Retrieval and chunking

Verified `arcmemory/config.py` defaults (set via `[modules.memory.config.dynamics]`):

| Knob | Default | Shapes |
|---|---|---|
| `curate_input` | `true` | whether input turns are curated into memory |
| `curate_conversation_kinds` | `{user, respond}` | which turn kinds are curated |
| `working_set_enabled` | `true` | the rolling recent-context set |
| `working_set_max` | `32` | max items in the working set |
| `working_set_decay_turns` | `5` | how fast working-set items age out |
| `doc_chunk_tokens` | `512` | document chunk size |
| `doc_chunk_overlap` | `0.10` | fractional overlap between chunks |
| `doc_rerank_margin` | `0.05` | rerank tie margin |
| `doc_search_top_k` | `10` | default document hits returned |
| `doc_search_min_score` | `0.0` | floor score for a returned hit |

Retrieval fuses channels with reciprocal-rank fusion:
`retrieve()` (`arcmemory/retrieve.py:95`) calls `_rrf_fuse` (`:114`, def `:200`)
over surface (vec + bm25 + graph + recency) and structural (trigger + cue)
channels; the shared combiner is `rrf_fuse` (`fusion.py:16`, `1/(k+rank)`,
`k=60`).

## The scope join — the isolation you don't configure

You can't turn this off, and that's the point. `vec0` carries no scope column, so
`vec_search` joins against `chunks WHERE scope = ?`
(`arcmemory/index/backend.py:258`) — *"the join is what stops a search scoped to
one agent from ever scoring another agent's vectors (LLM08)."* BM25 isolates the
same way (`WHERE scope=? AND fts_chunks MATCH ?`, `:279`). Per-source document
pools use the same mechanism (`<did>:doc:<source_id>`). Full rationale:
[Memory, the index, and scope](../concepts/memory-index-and-scope.md).

## The index backend — SQLite vs pgvector

Default is per-agent SQLite, shared-nothing, no server, and it **degrades
gracefully**: if `sqlite-vec` is absent, retrieval falls back to BM25 + graph and
**Index health** reports the degraded semantic channel — but only if something is
watching, so watch it.

Opt into a shared pgvector index only when you need it (`index_backend` field,
default `"sqlite"`, `arcmemory/config.py:221`):

- `ARC_MEMORY_INDEX_BACKEND=postgres` in `~/arc/.env` selects the postgres
  backend (written by `scripts/deploy-node.sh:401`).
- `ARC_MEMORY_PG_DSN` supplies the DSN, read at
  `arcmemory/index/backend.py:614` (`open_index_backend`), which needs the
  `asyncpg`/`pgvector` extra.

This is a **separate database** from ArcStore's operational store — see
[Provision the operational store](operational-store.md#enabling-the-optional-pgvector-memory-index).

## Enabling semantic retrieval

Vector recall needs an embedder. Set `embed_backend` / `embed_model` in
`[modules.memory.config]`. With no embedder, keyword and graph retrieval still
work; the semantic channel is simply reported as degraded rather than failing
silently.

---

**Next:** [Operate & troubleshoot](operate-and-troubleshoot.md) for index-health
and connector symptoms. **Why memory is built this way:**
[Memory, the index, and scope](../concepts/memory-index-and-scope.md) and
[The memory lifecycle](../walkthrough/07-memory-lifecycle.md).
