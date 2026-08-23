<div align="center">

# 🧠 arcmemory

### **Dual-Speed Analogical Memory for Agents**
*Markdown you can read is the truth. The database is a cache. Recall finds the pattern, not just the words.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-002550.svg)](https://opensource.org/licenses/Apache-2.0)
[![Tests](https://img.shields.io/badge/tests-650%2B-0055BC.svg)](#-status)
[![Coverage](https://img.shields.io/badge/coverage-91%25-003B82.svg)](#-status)
[![Strict mypy](https://img.shields.io/badge/mypy-strict-0073FE.svg)](#-status)
[![Glass-box](https://img.shields.io/badge/storage-markdown-F68D2E.svg)](#-whats-inside)
[![Recall](https://img.shields.io/badge/recall-structural-F68D2E.svg)](#-whats-inside)

</div>

---

## ✨ What is arcmemory?

`arcmemory` is the memory substrate of the Arc stack (SPEC-041). Human-readable markdown is
the source of truth, a per-agent SQLite file is a disposable index, the "sleep" consolidation
pass is a bounded reasoning **agent** rather than a blind prompt, and the differentiator is
*structural recall* — retrieving a past pattern the present situation instances, even with
zero surface-text overlap.

Most agent "memory" is a vector store: embed everything, retrieve by cosine similarity. That
finds text that *looks* like the query. It cannot find the *lesson* — the recurring mechanism
behind situations that share no words. And it grows without bound: nothing consolidates,
dedups, or forgets. `arcmemory` adds the missing recall channel, keeps every durable memory as
an inspectable, editable markdown file rather than an opaque blob, and treats consolidation as
real work.

It is the reference `Brain` adapter for Arc, but it depends on **no** agent framework. It
implements a *structural* four-method port, so a host plugs it in — or replaces it — without
either side importing the other.

Four commitments hold everything else up:

- **Glass-box.** Entities, insights, procedures, events, and daily notes are markdown you can
  read, grep, edit, and version. The SQLite index rebuilds from them byte-for-byte.
- **Dual-speed.** Capture is zero-LLM and constant-cost on the hot path. Reasoning happens off
  the hot path in a bounded "sleep" pass.
- **Non-lossy.** Contradictions fold into `| was:` trails; merges preserve both cards and
  record aliases. Nothing is silently overwritten.
- **Degrade, don't crash.** No embedder → recall falls back to BM25 + graph. No LLM, or a
  breached agentic budget → consolidation falls back to a deterministic pipeline. No `arcrun`
  installed → same fallback. Never a crash, and never silent (`arc memory status` reports it).

---

## ⭐ Top Features

What makes `arcmemory` different from vector-only memory systems:

### **Glass-Box Storage**
- **Markdown is the source of truth** — entities, insights, procedures, events, and daily notes are files you can read, diff, and hand-edit; no opaque blob to reverse-engineer
- **The index is disposable** — a corrupted or poisoned `index.db` is fixed with `wipe → rebuild`, and the result is byte-identical
- **Version-control friendly** — memory is a directory of markdown, so git gives you history and review for free

### **Dual-Speed Architecture**
- **Zero-LLM capture on the hot path** — sanitize → dedup → append → tag → Hebbian-bump; constant cost regardless of store size, no model call ever
- **Bounded single-pass recall** — one fused pass with a `top_k` + token budget; deliberately not an agentic re-query loop (LLM10)
- **Agentic sleep pass** — reasoning runs off the hot path over a bounded window, on a hard turn / token / wall-clock budget

### **Structural (Analogical) Recall**
- **Two channels, both required** — insight *triggers* matched in abstraction space **and** cue activation flowed over the learned graph; a candidate must clear both
- **Zero-overlap retrieval** — answers "what past pattern does this situation instance", not "what past text looks like this query"
- **Confidence-gated** — a `guessed` insight surfaces marked "verify first"; it becomes `known` only once corroborated

### **Proactive & Time-Aware Recall**
- **Working-set recall** — a bounded, decaying, salience-filtered per-session set of entities "in play" feeds the detectors, so a card surfaces for an entity named a *prior* turn
- **Mid-loop decision recall** — at a decision point (pre-plan by default, pre-tool opt-in) a relevant past decision reaches the model *between* loop steps, via `arcrun`'s append-only `transform_context` hook
- **Temporal reasoning** — cards carry *when* a memory was established; conflicting facts show current vs. superseded (never deleted); an optional window filters recall and recency breaks exact ties
- **One deterministic path** — every one of the above flows through `Brain.on_moment`, with no LLM or embedder on the trigger/rank path

### **Secure by Construction**
- **Untrusted text is defanged before it becomes memory** — NFKC normalization, invisible-character stripping, injection-pattern removal, secret redaction, windowed dedup
- **Signed → authorized → audited mutations** — every sleep-pass tool call is signed with the agent key, evaluated fail-closed by the policy pipeline, and audited exactly once
- **No-read-up recall** — every memory carries a classification label; recall drops anything the caller's clearance does not dominate, and audits the drop by hash, never plaintext
- **Shared-nothing isolation** — one DB file and one scope key per agent; vector search joins through `chunks` so no scope can see another's vectors

### **Pluggable Seams**
- **Brain port** — `capture` / `retrieve` / `consolidate` / `rebuild_index`; any class of that shape is a valid brain
- **Embedder / Distiller / Reranker / EntityDisambiguator** — injected Protocols; the core imports no provider
- **ReactLoop engine** — all agentic execution is confined to one adapter module, so a different harness is a sibling adapter
- **IndexBackend** — SQLite + sqlite-vec by default, PostgreSQL + pgvector as an opt-in implementation of the same typed contract

### **Connected Data Sources**
- **Three destinations, one mapping** — a synchronized source routes to memory, a per-source document pool, or a live datastore
- **Operator-approved routing** — a source-to-home mapping is staged as an approval row and is un-committable until an operator resolves it
- **Mutability-driven sync** — an immutable source never deletes; a mutable source tombstones; zero-trust count / byte / age caps bound any first backfill

---

## 🏗️ Where It Fits

```text
                    arcagent
                       │   selects a brain by name through
                       │   arcagent.brain.select_brain
                       │   (optional `arcagent[memory]` extra;
                       │    default brain is NullBrain)
                       ▼
    ┌──────────────────────────────────────────┐
    │  arcmemory                               │
    │  capture · retrieve · consolidate         │
    │  markdown stores + disposable index      │
    └──────────────────────────────────────────┘
        │          │          │         │        │
        ▼          ▼          ▼         ▼        ▼
    arctrust    arcllm    arcprompt  arcstore  arcrun
    identity    embed +   stock      approval  the ReAct
    policy      distill   prompts    rows +    sleep loop
    audit       seams                spool     (react_adapter
    classify                                    only)
```

`arcmemory` sits **below** `arcagent` and **never imports it** — enforced by
`tests/architecture/test_no_arcagent_import.py`. The dependency arrow only ever points
down: `arcagent` names a brain module by string, lazily imports it, and calls its
well-known `build_brain(context)` factory. `arcrun` is reachable from exactly one module
(`react_adapter.py`), also architecture-enforced, so an `arcrun`-less install degrades to
the deterministic pipeline instead of failing to import.

---

## 🚀 Install

```bash
pip install arcmemory               # core: sqlite-vec index + agentic sleep pass
pip install "arcmemory[local]"      # + on-device embedder (via arcllm[local])
pip install "arcmemory[docs]"       # + PDF / DOCX / XLSX document extractors
pip install "arcmemory[postgres]"   # + PostgreSQL / pgvector index backend
```

`sqlite-vec` ships as a base dependency, so the vector table always exists; the `[local]`
extra is what *fills* it. Without an embedder the vector channel is dropped and recall runs
on BM25 + graph — deliberate, never a crash, and no longer silent: it warns once per process
and `arc memory status` reports it (exit 1). Requires Python ≥ 3.11.

---

## 🧪 Quick Example

```python
import asyncio
from pathlib import Path

from arcmemory import ArcMemoryBrain

# One brain per agent workspace. Identity is mandatory — memory is DID-bound.
brain = ArcMemoryBrain(Path("./agent-workspace"), agent_did="did:arc:demo")


async def main() -> None:
    # Fast path — zero LLM, constant cost, sanitized before it is written.
    await brain.capture("Brad Baker is the CTO at CTG Federal.", kind="observation")

    # Recall — one bounded pass, classification-gated, returned as injectable
    # text already wrapped as untrusted DATA (boundary markers defanged).
    print(await brain.retrieve("who runs engineering at CTG?", top_k=5))

    # The structured cards behind that text — provenance + [[links]] intact.
    # This is what a host exposes as a first-class `recall` tool.
    for card in await brain.recall("who runs engineering at CTG?", top_k=5):
        print(card.source, card.kind, card.confidence, card.links)

    # Sleep pass — arcmemory decides internally which pass is due
    # (crash recovery / nightly hygiene / light consolidation / no-op).
    print(await brain.consolidate())

    # The index is a cache: this re-derives all of it from the markdown + stream.
    await brain.rebuild_index()


asyncio.run(main())
```

Wire the `model=`, `embedder=`, and `distiller=` seams to light up semantic + analogical
recall and the agentic sleep pass. With none of them, the example above still runs.

---

## 🧩 What's Inside

### The Brain port (`arcmemory.brain`, `arcmemory.provider`)

`ArcMemoryBrain` implements a small **structural** port a host can depend on without
importing `arcmemory` — dependency inversion is what makes a bring-your-own backend a
drop-in.

| Method | Speed | What it does |
|---|---|---|
| `capture(text, *, kind, salience, classification, session_id)` | fast, per-turn | sanitize → dedup → append raw event → tag entities → Hebbian-bump the graph. No LLM |
| `retrieve(query, *, clearance, top_k, budget, summary, cues, session_id) -> str` | fast, per-prompt | fuse surface + structural channels, gate on clearance, bound, return boundary-marked injectable text |
| `consolidate(*, session_id) -> Mapping` | slow, background | run the due pass (light consolidation / nightly hygiene / crash recovery); return mutation counts + an episode summary |
| `rebuild_index(*, session_id)` | maintenance | re-derive the disposable index from the markdown + stream |

Beyond the four primitives: `recall(...) -> list[RecallCard]` returns the structured
glass-box cards behind `retrieve()`, and `on_moment(...)` is the single proactive-recall
path (detected moment → deterministic detectors → dedup → gated cards). A brain is bound to
one `agent_did` + workspace; a per-call `session_id` narrows the shared-nothing scope.

`build_brain(context)` in `provider.py` is `arcmemory`'s side of `arcagent`'s generic seam:
the host passes a backend-agnostic context dict (workspace, DID, tier, audit sink, identity,
policy pipeline, opaque `backend_config`) and gets a fully wired brain back — so `arcagent`
never learns an `arcmemory` field name.

### The five markdown stores (`arcmemory.stores`)

```
<workspace>/memory/
  entities/<slug>.md      # people/places/projects — fact triplets + wiki-links
  insights/<id>.md        # minted patterns/theses — the centerpiece
  procedures/<slug>.md    # how-to methods distilled from the conversation
  events/<slug>.md        # what happened in the USER's life (meeting, sale, call)
  daily-log/YYYY-MM-DD.md # curated daily meeting-minutes (not a transcript)
  index.db                # disposable SQLite index
```

- **Semantic (`stores/semantic.py`).** One card per entity. Facts are triplet lines
  `predicate: value .confidence date`, with a `| was: prior` trail when a value changes —
  additive, never destructive. `[[slug]]` values become graph edges.
- **Insight (`stores/insight.py`).** The centerpiece. Each card carries a mechanism-level
  `trigger` (embedded into abstraction space), abstract `cues` (graph nodes), and
  `instances` (the episodes it generalizes). Starts `guessed`, becomes `known` once
  corroborated.
- **Procedural (`stores/procedural.py`).** Reusable **methods** the session revealed — how a
  thing is analyzed, decided, handled. Distilled by the LLM from the conversation and
  **evolved in place** as later sessions add, remove, or modify steps. Never mined from the
  agent's own tool activity.
- **Events (`stores/events.py`).** What happened in the *user's* life. Each card records when
  it occurred (distinct from when it was recorded), its type, the `[[participants]]` in it
  (shared-graph edges, so a person is one hop from their history), and how it came out.
- **Daily notes (`stores/daily.py`).** A curated per-day rollup (timeline, discussions,
  decisions, people, goals, tasks) written by the sleep pass — *not* a raw transcript.

The **raw episodic stream** (`stores/episodic.py`, a sixth, SQLite-only store) is the
audit-grade transcript every derived artifact is built from.

### The index (`arcmemory.db`, `arcmemory.index`)

`index.db` is one file per agent workspace — hard shared-nothing isolation (LLM08) — and is
entirely rebuildable from the markdown + stream by `index/rebuild.py`.

| Table | Holds |
|---|---|
| `episodic` | the raw append-only event stream |
| `chunks` | index provenance + per-chunk classification label |
| `fts_chunks` | FTS5 / BM25 keyword mirror |
| `vec0` | surface embedding vectors (present only when `sqlite-vec` loads) |
| `edges` | the weighted associative / semantic / cue graph (Hebbian + decay state) |
| `insight_trigger` | abstraction-space trigger vectors, kept apart from `vec0` |

`index/backend.py` puts the raw `chunks` / `fts_chunks` / `vec0` SQL behind an async
`IndexBackend` Protocol, selected by name from `MemoryConfig.index_backend`: `sqlite`
(default, per-agent file) or `postgres` (pgvector, a shared server). `vec_search` is
scope-isolated by joining through `chunks`, because `vec0` itself carries no scope column —
without that join one agent's chunk ids leak into another agent's vector search.
`asyncpg` / `pgvector` are lazy-imported, so a missing `[postgres]` extra is a clear
`RuntimeError` naming the extra, never an `ImportError`.

### Capture — the fast path (`arcmemory.capture`, `arcmemory.security`)

Every turn, `FastCapture` sanitizes untrusted text (`sanitize → privacy_filter → dedup`:
Unicode normalization, invisible-character and injection-pattern stripping, secret
redaction, a recent-hash window), appends a raw event, tags known entities
deterministically, strengthens the co-occurrence graph with a saturating Hebbian update,
and emits a `memory.captured` audit event. This module imports no `arcllm` and issues no
embedding — capture is pure CPU/IO, constant cost regardless of store size.

`security.py` is the ASI06 (memory poisoning) / LLM01 (prompt injection) boundary. It also
owns `gate_no_read_up`, which maps a memory's label onto the `arctrust` ladder and calls
`arctrust.dominates` — `arcmemory` reuses Arc's comparator and defines none of its own.

### Retrieval (`arcmemory.retrieve`, `arcmemory.index`)

One bounded pass fuses two channels with Reciprocal Rank Fusion:

- **Surface (`index/surface.py`)** — vector cosine + BM25 + graph spreading-activation +
  recency. Answers "what past text looks like this query."
- **Structural (`index/structural.py`)** — the abstracted situation is matched against
  insight *triggers* (embedding) **and** cue activation is flowed over the learned graph to
  insight nodes. A candidate must clear **both**. Answers "what past *pattern* does this
  situation instance," even with zero surface overlap.

Results are confidence-gated (`guessed` → "verify first"), no-read-up gated against the
caller's clearance, bounded to `top_k` + a token budget, and wrapped as untrusted DATA
(forged boundary markers defanged) before injection. Recall is a fast pipeline, never a
sub-agent — a host drives deeper, iterative recall by calling the recall **tool** from its
own loop.

`detectors.py` holds the deterministic detected-moment gate and per-session window dedup
that decide *whether* a proactive recall fires and *which* cards it may inject. No embedder
and no LLM ever touch that path.

### Consolidation — the sleep pass (`arcmemory.consolidate`, `arcmemory.agent_consolidate`)

Off the hot path, over a bounded window of the stream, the engine distills the **session
conversation only** — `curate.py` deterministically filters out tool frames and runtime
plumbing, so the agent's own mechanics never become memory. It extracts facts (additive,
corroboration-grown confidence), mints insights, distills reusable procedures, records life
events, decays unreinforced edges, merges near-duplicate cues, de-duplicates entities, and
reindexes what it touched.

**Entity de-duplication is confirm-gated, never automatic.** Same-type cards are clustered
by name-embedding into *candidate* groups (a wide net), then **one bounded LLM call per
cluster** decides which are truly the same real-world entity — so "Josh Schultz" and "Joshua
Shubbie" stay separate even though they embed alike. Only confirmed groups fold, non-lossily.
With no embedder or no confirmer wired, it emits a loud `memory.dedup_skipped` audit rather
than silently doing nothing.

Once per local day the first pass escalates to **nightly hygiene** (`hygiene.py`): alias
merge, reciprocal backlink repair, and workspace dedup — all idempotent and file-driven.
`arcmemory` owns cadence: the host polls `consolidate()` and `arcmemory` decides internally
whether to recover a crashed run, run hygiene, run the light pass, or no-op. A write-ahead
manifest makes any interrupted run crash-safe.

**Agentic by default.** `agent_consolidate.py` runs a bounded ReAct loop over a registry of
memory tools (`search_similar_entity`, `read_card`, `recall_surface`, `neighbors`,
`write_fact`, `merge_entities`, `link`, `record_insight`, `record_procedure`, `set_alias`),
so the engine searches existing memory before writing, merges duplicates with judgment,
follows and builds links, and self-verifies — instead of emitting one unconditioned
extraction. On a breach, timeout, missing model, or `arcrun`-less install, the same window
is finished by the deterministic single-pass pipeline distiller: no data loss, never a crash.

### Memory tools (`arcmemory.tools`, `arcmemory.react_adapter`)

The sleep-pass agent reaches durable memory **only** through wrapped tools. Each `execute`
is secured exactly the way a first-class agent tool is:

1. build a `ToolCall` (agent DID, session, classification);
2. **sign** it with the memory-agent identity — an unsigned call is denied fail-closed;
3. `await policy_pipeline.evaluate(...)` — first-DENY-wins, and any exception is caught and
   treated as a deny;
4. only on ALLOW does the store mutate;
5. emit exactly one tamper-evident `AuditEvent` per call (allow / deny / error).

Tools are individually atomic and audited, so partial agentic progress is always safe.
`react_adapter.py` is the **only** module that imports `arcrun`; it maps an `ImportError`,
a wall-clock timeout, or a bounded-loop breach onto a `ReactOutcome(degraded=True)` the
caller falls back from.

### Distillation seams (`arcmemory.distill`, `arcmemory.arcllm_seam`)

The embed / distill / rerank / disambiguate primitives are injected behind Protocols
(`Embedder`, `Distiller`, `Reranker`, `EntityDisambiguator`). The core imports no provider;
production injects the `arcllm`-backed `ArcLLMEmbedder` / `ArcLLMDistiller`, and tests
inject a deterministic fake. `degrade.py` makes a dead semantic channel **loud and
once-per-process** — a `WARNING` naming the dead channel and the fix, plus a process-wide
flag `arc memory status` reads directly.

### Links & updates (`arcmemory.index.graph`, `arcmemory.hygiene`)

- **Links.** A `[[slug]]` in a fact value creates a directed graph edge and records the link
  in the source card's `links_to`. Nightly hygiene repairs the reciprocal backlink into the
  target, so relationships are navigable from both ends. The graph carries three edge kinds:
  `assoc` (co-occurrence, Hebbian), `link` (wiki-links), and `cue` (insight → cue node).
- **Updates.** Facts upsert by canonical slug. An unchanged value grows more confident with
  corroboration (`1 − e^(−γ·hits)`); a changed value is a contradiction folded into a
  `| was:` trail, never an overwrite. Entity and cue merges are non-lossy — facts union, the
  higher-confidence value stays current, the losing card's name is preserved as an alias,
  and graph edges follow the survivor.
- **Identity (search-before-write).** Before minting an entity, `arcmemory` resolves it:
  exact file → recorded alias → same-type embedding match → a bounded disambiguation call →
  otherwise new. The deterministic first two steps run with no model, closing the loop that
  otherwise mints "Austin, Texas" and "Austin, TX" as separate cards.

### Connected data sources (`arcmemory.ingest`, `arcmemory.doc_index`, `arcmemory.datastore`)

An external source is normalized by an adapter (`adapter.py`), fanned to one-or-more
destinations by `router.py`, and lands in one of five homes — `memory`, `document`,
`datastore`, `blob`, or a review-gated `profile`.

| Module | Role |
|---|---|
| `ingest.py` | Brain-port ingestion: zero-trust batch cap, content-hash dedup, deterministic per-`(source, external_id)` upsert key, last-writer-wins ordering |
| `doc_index.py` | Per-source **document pool** — a scope isolated from memory recall, so a source-bounded search can never surface another source's chunks. Stores chunk text + a pointer back to the object; raw file bytes are never stored |
| `datastore.py` | Backend-neutral structured-data port + a stdlib-only SQLite adapter. Every interpolated identifier is checked against the schema it introspected itself; every value is a bound parameter; no public method executes a raw SQL string |
| `mapping.py` | A source-to-home mapping is staged as an operator approval row and is un-committable until resolved `approved` (fail-closed). Once approved it persists as ordinary facts with the same additive `was:` trail |
| `sync.py` | Pure decision engine: an immutable source never deletes, a mutable source tombstones; count / byte / age caps bound a first backfill (LLM10) |
| `chunk.py`, `extract.py` | Recursive chunking + lazy PDF / DOCX / XLSX extractors (the `[docs]` extra; absence raises `ExtractionUnavailable`, never a crash) |

### Operator surface (`arcmemory.operator`, `arcmemory.status`, `arcmemory.timeline`)

`MemoryOperator` is the one seam a UI consumes: paged list / get over episodic memories with
curator metadata (created timestamp, decay indicator, a 1..10 importance projection of stored
salience, daily-log source), entities and links for graph navigation, search that delegates
to the production `Retriever` so ranking matches recall exactly, and edit / set-metadata /
delete mutations that carry the actor DID and return an honest `MutationResult` — applied or
error, never a partial success. No consumer runs SQL against `index.db`.

`timeline.py` answers "what changed over a period" by reading the already-stored, timestamped
event and daily-log cards in chronological order — it adds no new store, and gates every
entry with the same no-read-up predicate recall uses.

---

## ⚙️ Configuration

`arcmemory` reads a frozen `MemoryConfig`. `MemoryConfig.for_tier("personal" | "enterprise" |
"federal")` returns the tuned constant set — federal writes slower, decays slower, demands
more corroboration, and tightens the agentic-loop budget. Tier is stringency metadata, not a
feature gate: every tier still captures, gates, decays, and audits.

Selected fields (see `config.py` for the full set): `alpha` / `saturation` (Hebbian write);
`lambda_fast` / `beta` / `forget_floor` (decay); `gamma` / `known_threshold` (confidence);
`fan_strength` / `max_hops` (spreading activation); `entity_merge_threshold` /
`entity_disambiguate_min` (identity); `struct_trigger_min` / `struct_activation_min` /
`rerank_margin` (structural recall); `consolidate_interval_minutes` /
`distill_max_input_tokens` (consolidation); `index_backend` (`sqlite` / `postgres`); and the
agentic-engine knobs `consolidate_engine` (`"agentic"` default / `"pipeline"`) with
`consolidate_agent_max_turns` / `consolidate_agent_max_tokens` /
`consolidate_agent_timeout_seconds`.

Proactive & temporal recall, all with default-safe off-switches: `working_set_enabled`
(default `True`) gates the per-session working set feeding the proactive detectors;
`working_set_max` (`32`) bounds it and `working_set_decay_turns` (`5`) ages entries out;
`temporal_enabled` (default `True`) gates when-established / supersession / time-window /
recency-tiebreak behavior on recall cards.

When driven through Arc, the host exposes a thin `[modules.memory]` block — `brain`
(`none` / `arcmemory` / `auto` / a BYO `module:Class`), `tier`, `embed_backend` /
`embed_model`, `distill_provider` / `distill_model`, `top_k` / `budget`, and the
consolidation triggers (`consolidate_event_threshold`, `consolidate_idle_seconds`,
`consolidate_interval_seconds`). The same block adds `working_set_enabled` plus two
decision-point off-switches, both default `False`: `proactive_decision_point` (arm mid-loop
recall at all) and `decision_point_pre_tool` (extend it to the pre-tool site; pre-plan is the
default site when armed).

---

## 🖥️ CLI

| Command | What it does |
|---|---|
| `arc memory dedup [--apply] <workspace>...` | Merge pre-canonicalization duplicate cards into their canonical file. Dry-run by default; `--apply` writes and deletes variants (idempotent) |
| `arc memory status [--backend local\|provider\|none]` | Report whether semantic (vector) recall is live or degraded to BM25 + graph. Exit 1 when degraded |
| `arc memory backend [--backend sqlite\|postgres] [--dsn ...]` | Report whether the configured index backend is reachable |
| `arc memory okf-migrate [--apply] <workspace>...` | Re-render pre-OKF memory documents through the canonical OKF writer |
| `arc agent memory [--path <dir>] [--limit N] [--json]` | Read-only view of an agent's memory DB (episodic stream, counts, top graph associations). Opens the DB read-only; never writes |

Every one of these delegates to `arcmemory` itself — no memory-maintenance logic lives in the
CLI, so a deployment can swap the whole package out.

---

## 🧷 Pluggability (bring-your-own / disable)

Any class matching the four-method `Brain` shape is a valid brain, so a host can:

- **use arcmemory** (`brain = "arcmemory"`),
- **bring its own** backend (`brain = "yourpkg:YourBrain"`, allowlist-gated above the
  personal tier), or
- **run memory-less** (`brain = "none"` → a no-op brain, zero files).

Within `arcmemory`, the LLM / vector / execution / storage work is itself injected — swap the
`Embedder`, `Distiller`, `Reranker`, `IndexBackend`, or the `ReactLoop` engine seam without
touching the core.

---

## 🤝 Personal and fleet-shared knowledge

`arcmemory` owns team-agnostic collection mechanics: canonical OKF documents, explicit scope,
and the store / index / embed / search / provenance / revoke contracts, plus classification
checks. `arcteam` owns membership, promotion authorization, shared lifecycle, and the fleet
backend — through those public contracts only. It cannot make an agent workspace or private
memory global.

`SharedKnowledgeAdapter` is a generic collection adapter, not a fleet service. `arcteam`
attaches the fleet tools only to authorized started members through its extension lifecycle;
`arcmemory` neither imports `arcteam` nor exposes fleet tools. If the optional collection
mechanics are absent, `arcteam` reports typed unavailability rather than creating a local
shared substitute.

| Tool (attached by arcteam) | Purpose |
|---|---|
| `shared_knowledge_promote(reference)` | Promote owned personal knowledge into the signed fleet collection |
| `shared_knowledge_retrieve(reference)` | Retrieve one authorized fleet document |
| `shared_knowledge_search(query)` | Search fleet knowledge at the caller's clearance |
| `shared_knowledge_revoke(reference)` | Revoke an owned fleet document without erasing audit evidence |

See [SETUP.md](SETUP.md#5-signed-fleet-shared-knowledge) and the repository
[fleet-layering guide](../../docs/concepts/fleet-layering.md).

---

## 🛡️ Security Properties

| Property | How |
|---|---|
| **Untrusted input is defanged before it becomes memory** | NFKC normalization, zero-width / invisible / control stripping, injection-pattern removal, secret redaction, and windowed dedup on every capture |
| **Signed, authorized, audited mutations** | Every sleep-pass tool call is signed with the agent key, evaluated first-DENY-wins and fail-closed on any exception, and audited once. Unsigned or denied writes never mutate the store |
| **No-read-up recall** | Each memory carries a classification label; recall drops anything the caller's clearance does not dominate (Bell-LaPadula). Federal strictness fails closed on an unlabeled memory; every drop is audited by hash, never plaintext |
| **Injection-inert injection** | Retrieved memories are wrapped as untrusted DATA with defanged boundary markers, so a poisoned memory cannot break out of its block |
| **Shared-nothing isolation** | One DB file and one scope key per agent; `vec_search` joins through `chunks` so no scope-free vector table can leak another agent's ids |
| **Bounded by construction** | Recall is single-pass with `top_k` + token budget; the sleep loop is capped on turns, tokens, and wall clock; ingestion caps batch count, bytes, and age (LLM10) |
| **Reuses Arc's comparator** | `arcmemory` defines no classification ladder of its own — it calls `arctrust.dominates` (architecture-enforced) |

---

## 📊 Comparison to other memory systems

A condensed, honestly-sourced comparison. `arcmemory` is young and unbenchmarked; incumbents
lead on ecosystem, managed scale, and published retrieval benchmarks.

| System | Source of truth | Recall | Consolidation & dedup | Local-first | Security / audit built-in |
|---|---|---|---|---|---|
| **arcmemory** | **Markdown cards** (index is disposable) | vector + graph + **structural/analogical** | **agentic** sleep pass; non-lossy merge, `\|was:` trails | **Yes**, no service required | **Per-op DID sign + policy + audit** in the substrate |
| Letta (MemGPT) | Opaque DB tiers | vector + keyword | agent self-edits memory | self-host or cloud | standard OSS hosting |
| mem0 | Vector rows + entity collection | vector + keyword + entity | single-pass LLM extraction | self-host or cloud | audit is Enterprise-tier only |
| Zep / Graphiti | Temporal knowledge graph | semantic + BM25 + graph, bi-temporal | LLM extraction + temporal invalidation | Graphiti OSS; Zep Cloud hosted-first | SOC2 / HIPAA on the *hosted* product |
| LangMem | Storage-agnostic (usually a DB) | backend-dependent | background extract / consolidate | either (library) | inherits deployment |
| Cognee | Graph + vector + relational | 14 retrieval modes | 6-stage ECL pipeline; `forget` | embedded or cloud | not compliance-positioned |
| Vector-RAG baseline | Opaque vectors | cosine only | **none** (grows unbounded) | yes | none built-in |

**Where arcmemory is genuinely differentiated:** glass-box markdown source-of-truth (every
surveyed system stores memory opaquely); non-lossy update history; structural / analogical
recall; and identity / signing / policy / audit built into the memory substrate itself rather
than bolted on at a hosted-product layer — a real edge for regulated / federal deployments.

**Where incumbents lead:** ecosystem, integrations, and community (mem0, Letta); managed /
autoscaled hosting (Zep / mem0 / Letta / Cognee Cloud); and **published retrieval benchmarks**
(mem0 and Zep report LoCoMo / LongMemEval numbers). `arcmemory` has no public benchmark yet.

*Competitor claims (2025–2026) are sourced from public docs and repos; vendor benchmark
figures are self-reported with inconsistent methodologies — treat as directional.*

---

## 🧪 Status

```bash
uv run --no-sync pytest packages/arcmemory/tests
```

- **Tests:** 650+
- **Coverage:** 91%
- **Type check:** `mypy --strict` clean
- **Lint:** `ruff check` clean
- **Version:** `0.9.0`, **alpha** — API may still shift

All SPEC-041 phases have landed (zero-LLM capture, surface + structural recall, the agentic
sleep pass with signed memory tools + pipeline fallback, nightly hygiene, search-before-write
identity resolution, no-read-up recall), along with proactive / temporal recall and connected
data-source ingestion. Adversarial fail-closed security tests are part of the suite.

Known limitations, stated plainly:

- **Structural / analogical recall is unproven.** It is architecturally real but has **no
  published benchmark** (LoCoMo / LongMemEval, etc.). Treat it as *different*, not yet
  *better*, until measured — a benchmark harness is the next release gate.
- **No managed hosting.** Local-first is a deliberate tradeoff; there is no autoscaled cloud
  option.
- **Young ecosystem.** No third-party integrations or track record at scale yet.

---

## 📄 License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
