# arcmemory

**Dual-speed, glass-box, analogical memory for AI agents.** Human-readable markdown
is the source of truth, a per-agent SQLite file is a disposable index, the "sleep"
consolidation pass is a bounded reasoning **agent** (not a blind prompt), and the
differentiator is *structural recall* — retrieving a past pattern the present
situation instances even with zero surface-text overlap.

arcmemory is the reference `Brain` adapter for [Arc](https://github.com/joshuamschultz/Arc),
but it depends on **no** agent framework — it plugs into anything that speaks its
four-method port, or runs standalone.

---

## Why

Most agent "memory" is a vector store: embed everything, retrieve by cosine
similarity. That finds text that *looks* like the query. It cannot find the
*lesson* — the recurring mechanism behind situations that share no words. And it
grows without bound: nothing consolidates, dedups, or forgets. arcmemory adds the
missing recall channel, keeps every durable memory as an inspectable, editable
markdown file rather than an opaque blob, and treats consolidation as real work.

Design commitments:

- **Glass-box.** Entities, insights, procedures, and daily notes are markdown you
  can read, grep, edit, and version. The SQLite index rebuilds from them byte-for-byte.
- **Dual-speed.** Capture is zero-LLM and constant-cost on the hot path. Reasoning
  happens off the hot path in a bounded "sleep" pass.
- **Non-lossy.** Contradictions fold into `| was:` trails; merges preserve both
  cards and record aliases. Nothing is silently overwritten.
- **Secure by construction.** Every capture is sanitized against injection and
  secret leakage; every recall is classification-gated (no-read-up); every mutation —
  including every tool call the sleep-pass agent makes — is signed, authorized, and audited.
- **Degrades, never fails.** No embedder → recall falls back to BM25 + graph. No LLM,
  or the agentic loop breaches its budget → consolidation falls back to a deterministic
  pipeline. No arcrun installed → same fallback. Never a crash.

---

## Install

```bash
pip install arcmemory              # core: in-process vector index + agentic sleep pass
pip install "arcmemory[local]"     # + offline local embedder (sentence-transformers)
```

`sqlite-vec` ships by default, so semantic recall works out of the box; it is still
load-guarded, so an environment without it degrades to BM25 + graph rather than
failing. Requires Python ≥ 3.11.

---

## Quickstart

```python
import asyncio
from pathlib import Path
from arcmemory import ArcMemoryBrain

brain = ArcMemoryBrain(Path("./agent-workspace"), agent_did="did:arc:demo")

async def main():
    # Fast path — zero LLM, constant cost.
    await brain.capture("Brad Baker is the CTO at CTG Federal.", kind="observation")

    # Recall — single bounded pass, classification-gated, returns injectable text.
    print(await brain.retrieve("who runs engineering at CTG?", top_k=5))

    # Sleep pass — arcmemory decides internally which pass is due.
    print(await brain.consolidate())

asyncio.run(main())
```

Wire the `model=`, `embedder=`, and `distiller=` seams (see *The consolidation
engine* and *Pluggability*) to light up semantic + analogical recall and the
agentic sleep pass. With none of them, the example above still runs.

---

## Architecture

### The Brain port (four primitives)

arcmemory implements a small **structural** port an agent framework can depend on
without importing arcmemory (dependency inversion — this is what makes a bring-your-own
backend a drop-in):

| Method | Speed | What it does |
|---|---|---|
| `capture(text, *, kind, salience, classification, session_id)` | fast, per-turn | sanitize → dedup → append raw event → tag entities → Hebbian-bump the graph. No LLM. |
| `retrieve(query, *, clearance, top_k, budget, summary, cues, session_id) -> str` | fast, per-prompt | fuse surface + structural channels, gate on clearance, bound, return boundary-marked injectable text. |
| `consolidate(*, session_id) -> Mapping` | slow, background | run the due pass (light consolidation / nightly hygiene / recovery); return mutation counts + an episode summary. |
| `rebuild_index(*, session_id)` | maintenance | re-derive the disposable index from the markdown + stream. |

`ArcMemoryBrain` is bound to one `agent_did` + workspace (identity is mandatory). A
per-call `session_id` narrows the shared-nothing scope. `recall(...) -> list[RecallCard]`
returns the structured glass-box cards (provenance + `[[links]]`) behind the injectable
`retrieve()` text — this is what a host exposes as a first-class recall **tool**.

### The four stores (glass-box markdown)

```
<workspace>/memory/
  entities/<slug>.md      # people/places/projects — fact triplets + wiki-links
  insights/<id>.md        # minted patterns/theses — the centerpiece
  procedures/<slug>.md    # how-to methods distilled from the conversation
  daily-log/YYYY-MM-DD.md # curated daily meeting-minutes (not a transcript)
  index.db                # disposable SQLite index (see below)
```

- **Semantic (entities).** One card per entity. Facts are triplet lines
  `predicate: value .confidence date`, with a `| was: prior` trail when a value
  changes — additive, never destructive. `[[slug]]` values become graph edges.
- **Insight (centerpiece).** Each card carries a mechanism-level `trigger` (embedded
  into abstraction space), abstract `cues` (graph nodes), and `instances` (the episodes
  it generalizes). Starts `guessed`, becomes `known` once corroborated.
- **Procedural.** Reusable **methods** — explicit or implicit ways of doing something
  the session revealed (how a thing is analyzed, decided, handled). Distilled by the LLM
  from the conversation and **evolved in place** as later sessions add / remove / modify
  steps. Never mined from tool/agentic activity.
- **Daily notes.** A curated per-day rollup (timeline, discussions, decisions, people,
  goals, tasks) written by the sleep pass. It is *not* a raw per-turn transcript — the
  raw stream stays in the episodic store and is never duplicated here.

The **raw episodic stream** (a fifth, SQLite-only store) is the audit-grade transcript
every derived artifact is built from.

### The index (disposable SQLite)

`index.db` is one file per agent workspace (hard shared-nothing isolation) and is
**entirely rebuildable** from the markdown + stream:

| Table | Holds |
|---|---|
| `episodic` | the raw append-only event stream |
| `chunks` | index provenance + per-chunk classification label |
| `fts_chunks` | FTS5 / BM25 keyword mirror |
| `vec0` | surface embedding vectors (present only when sqlite-vec loads) |
| `edges` | the weighted associative / semantic / cue graph (Hebbian + decay state) |
| `insight_trigger` | abstraction-space trigger vectors, kept apart from `vec0` |

Because the files are truth and the index is a cache, a corrupted or poisoned index
is fixed with `wipe → rebuild` and the result is byte-identical.

---

## How memory is captured, retrieved, and consolidated

### Capture (fast path)
Every turn, `capture()` sanitizes untrusted text (Unicode normalization,
invisible-character and injection-pattern stripping, secret redaction), dedups it
against a recent-hash window, appends a raw event, tags known entities
deterministically, and strengthens the co-occurrence graph with a saturating Hebbian
update. No model call, constant cost regardless of store size.

### Retrieve (recall) — fast, never a sub-agent
One bounded pass fuses two channels with Reciprocal Rank Fusion:

- **Surface:** vector cosine + BM25 + graph spreading-activation + recency. Answers
  "what past text looks like this query."
- **Structural / analogical:** the abstracted situation is matched against insight
  *triggers* (embedding) **and** cue activation is flowed over the learned graph to
  insight nodes (spreading activation). A candidate must clear **both** channels.
  Answers "what past *pattern* does this situation instance," even with zero surface overlap.

Results are confidence-gated (`guessed` → "verify first"), no-read-up gated against
the caller's clearance, bounded to `top_k` + a token budget, and wrapped as untrusted
DATA (forged boundary markers defanged) before injection. Recall is a fast pipeline —
the host drives deeper, iterative recall by calling the recall **tool** from its own
agent loop, not by arcmemory spinning a sub-agent on the hot path.

### Consolidate (the sleep pass) & nightly hygiene
Off the hot path, over a bounded window of the stream, the engine distills the **session
conversation only** (the user's turns + the agent's responses — tool frames and other
machinery are filtered out deterministically, so the agent's own mechanics never become
memory): it extracts facts (additive, corroboration-grown confidence), mints insights,
distills reusable methods (procedures), and summarizes each day into curated notes. It
also decays unreinforced edges and de-duplicates entities. Once per local day the first
pass escalates to **nightly hygiene**: alias merge, reciprocal backlink repair, and
workspace dedup — all idempotent and file-driven.

**Entity de-duplication is confirm-gated, never automatic.** Same-type cards are
clustered by name-embedding into *candidate* groups (a wide net), then **one bounded LLM
call per cluster** decides which are truly the same real-world entity — so "Josh Schultz"
and "Joshua Shubbie" stay separate even though they embed alike. Only confirmed groups
fold (non-lossy). With no embedder or no confirmer wired, it emits a loud
`memory.dedup_skipped` audit rather than silently doing nothing.

arcmemory owns cadence: the host polls `consolidate()`; arcmemory decides internally
whether to recover a crashed run, run nightly hygiene, run the light pass, or no-op. A
write-ahead manifest makes any interrupted run crash-safe — recovery rebuilds the
disposable index from the files that landed.

---

## The consolidation engine (agentic by default)

The sleep pass — the one place arcmemory *reasons* over raw episodes — is a **bounded
agent**, not a blind one-shot prompt. Given a window of recent episodes, it runs a
ReAct loop with a registry of **memory tools** (`search_similar_entity`, `read_card`,
`recall_surface`, `neighbors`, `write_fact`, `merge_entities`, `link`, `record_insight`,
`record_procedure`, `set_alias`), so it can search existing memory before writing,
merge duplicates with judgment, follow and build links, and self-verify — instead of
emitting a single unconditioned extraction. This is the differentiator versus
single-pass extraction pipelines.

Every tool call is secured the same way a first-class agent tool is:

1. a `ToolCall` is built and **signed** with the agent's key (unsigned ⇒ deny);
2. it is evaluated by the **policy pipeline** (first-DENY-wins, **fail-closed** — any
   exception denies);
3. only on ALLOW does the store mutate; and
4. exactly one tamper-evident **audit event** is emitted per call (allow / deny / error).

The loop is **hard-bounded** (turns, tokens, and a wall-clock timeout) and **degrades
cleanly**: on breach, timeout, no model wired, or an arcrun-less install, the same
window is finished by a deterministic single-pass **pipeline** distiller — no data loss,
never a crash. Tools are individually atomic and audited, so partial agentic progress is
always safe.

**Engine portability.** All agentic-loop execution is confined to a single adapter
module (`react_adapter.py`) behind a `ReactLoop` seam — the only place the runtime
(arcrun) is imported, enforced by an architecture test. Running the sleep pass under a
different harness is a sibling adapter, not a package-wide change.

The embed / distill / rerank / disambiguate primitives are injected behind Protocols
(`Embedder`, `Distiller`, `Reranker`, `EntityDisambiguator`); the core imports no
provider, production injects an arcllm-backed adapter, and tests inject a deterministic
fake.

---

## How links & updates work

- **Links.** A `[[slug]]` reference in a fact value creates a directed graph edge and
  records the link in the source card's `links_to`. Nightly hygiene repairs the
  reciprocal backlink into the target, so relationships are navigable from both ends.
  The graph carries three edge kinds — `assoc` (co-occurrence, Hebbian), `link`
  (wiki-links), and `cue` (insight → cue node).
- **Updates.** Facts upsert by canonical slug. An unchanged value grows more confident
  with corroboration (`1 − e^(−γ·hits)`); a changed value is a contradiction folded into
  a `| was:` trail, never an overwrite. Entity and cue merges are non-lossy — facts
  union, the higher-confidence value stays current, the losing card's name is preserved
  as an alias, and graph edges follow the survivor.
- **Identity (search-before-write).** Before minting an entity, arcmemory resolves it:
  exact file → recorded alias → same-type embedding match → a bounded disambiguation call
  → otherwise new. The deterministic first two steps run with no model, closing the loop
  that otherwise mints "Austin, Texas" and "Austin, TX" (or "Josh" / "Joshua Schultz") as
  separate cards.

---

## Comparison to other memory systems

A condensed, honestly-sourced comparison. arcmemory is young and unbenchmarked;
incumbents lead on ecosystem, managed scale, and published retrieval benchmarks.

| System | Source of truth | Recall | Consolidation & dedup | Local-first | Security / audit built-in |
|---|---|---|---|---|---|
| **arcmemory** | **Markdown cards** (index is disposable) | vector + graph + **structural/analogical** | **agentic** sleep pass; non-lossy merge, `\|was:` trails | **Yes**, no service required | **Per-op DID sign + policy + audit** in the substrate |
| Letta (MemGPT) | Opaque DB tiers | vector + keyword | agent self-edits memory | self-host or cloud | standard OSS hosting |
| mem0 | Vector rows + entity collection | vector + keyword + entity | single-pass LLM extraction | self-host or cloud | audit is Enterprise-tier only |
| Zep / Graphiti | Temporal knowledge graph | semantic + BM25 + graph, bi-temporal | LLM extraction + temporal invalidation | Graphiti OSS; Zep Cloud hosted-first | SOC2 / HIPAA on the *hosted* product |
| LangMem | Storage-agnostic (usually a DB) | backend-dependent | background extract / consolidate | either (library) | inherits deployment |
| Cognee | Graph + vector + relational | 14 retrieval modes | 6-stage ECL pipeline; `forget` | embedded or cloud | not compliance-positioned |
| Vector-RAG baseline | Opaque vectors | cosine only | **none** (grows unbounded) | yes | none built-in |

**Where arcmemory is genuinely differentiated:** glass-box markdown source-of-truth
(every surveyed system stores memory opaquely); non-lossy update history; structural /
analogical recall; and identity / signing / policy / audit built into the memory
substrate itself rather than bolted on at a hosted-product layer — a real edge for
regulated / federal deployments.

**Where incumbents lead:** ecosystem, integrations, and community (mem0, Letta);
managed / autoscaled hosting (Zep / mem0 / Letta / Cognee Cloud); and **published
retrieval benchmarks** (mem0 and Zep report LoCoMo / LongMemEval numbers). arcmemory has
no public benchmark yet — see *Honest status*.

> Choose arcmemory when you need auditable / editable memory, non-lossy history, or
> substrate-level security for a regulated / local-first deployment. Choose mem0 for the
> broadest integrations and a mature default; Zep / Graphiti for a dedicated temporal
> knowledge graph; Letta / Cognee for strong self-hosted OSS with managed options.

*Competitor claims (2025–2026) are sourced from public docs and repos; vendor benchmark
figures are self-reported with inconsistent methodologies — treat as directional.*

---

## Configuration

arcmemory reads a frozen `MemoryConfig`; `MemoryConfig.for_tier("personal" |
"enterprise" | "federal")` returns the tuned constant set (federal writes slower, decays
slower, demands more corroboration, and tightens the agentic-loop budget). Tier is
stringency metadata, not a feature gate — every tier still captures, gates, decays, and
audits.

Selected fields (see `config.py` for the full set): `alpha` / `saturation` (Hebbian
write); `lambda_fast` / `beta` / `forget_floor` (decay); `gamma` / `known_threshold`
(confidence); `fan_strength` / `max_hops` (spreading activation); `entity_merge_threshold`
/ `entity_disambiguate_min` (identity); `struct_trigger_min` / `struct_activation_min` /
`rerank_margin` (structural recall); `consolidate_interval_minutes` /
`distill_max_input_tokens` (consolidation); and the agentic-engine knobs
**`consolidate_engine`** (`"agentic"` default / `"pipeline"`) with
`consolidate_agent_max_turns` / `consolidate_agent_max_tokens` /
`consolidate_agent_timeout_seconds`.

When driven through Arc, the host exposes a thin `[modules.memory]` block — `brain`
(`none` / `arcmemory` / `auto` / a BYO `module:Class`), `tier`, `embed_backend` /
`embed_model`, `distill_provider` / `distill_model`, `top_k` / `budget`, and the
consolidation triggers (`consolidate_event_threshold`, `consolidate_idle_seconds`,
`consolidate_interval_seconds`).

---

## CLI

- **`arc memory dedup [--apply] <workspace>...`** — merge pre-canonicalization duplicate
  cards into their canonical file. Dry-run by default; `--apply` writes and deletes
  variants (idempotent). Delegates entirely to `arcmemory.hygiene`.
- **`arc agent memory [--path <dir>] [--limit N] [--json]`** — a read-only view of an
  agent's memory database (episodic stream, counts, top graph associations). Opens the
  DB read-only; never writes.

---

## Pluggability (bring-your-own / disable)

arcmemory implements a structural `Brain` port with four methods. Any class matching
that shape is a valid brain, so a host can:

- **use arcmemory** (`brain = "arcmemory"`),
- **bring its own** memory backend (`brain = "yourpkg:YourBrain"`, allowlist-gated above
  the personal tier), or
- **run memory-less** (`brain = "none"` → a no-op brain, zero files).

Within arcmemory, the LLM / vector / execution work is itself injected — swap the
`Embedder`, `Distiller`, `Reranker`, or the `ReactLoop` engine seam without touching the
core.

---

## Security posture

- **Untrusted input is defanged before it becomes memory:** NFKC normalization,
  zero-width / invisible / control stripping, prompt-injection-pattern removal, secret
  redaction, and windowed dedup.
- **Signed, authorized, audited mutations:** every sleep-pass memory-tool call is signed
  with the agent key, evaluated by the policy pipeline (first-DENY-wins, fail-closed on
  any exception), and audited — unsigned or denied writes never mutate the store.
- **No-read-up recall:** each memory carries a classification label; recall drops
  anything the caller's clearance does not dominate (Bell-LaPadula). Federal strictness
  fails closed on an unlabeled memory; every drop is audited by hash, never plaintext.
  arcmemory reuses Arc's classification comparator and defines none of its own.
- **Injection-inert injection:** retrieved memories are wrapped as untrusted DATA with
  defanged boundary markers, so a poisoned memory cannot break out of its block.
- **Shared-nothing isolation:** one DB file and one scope key per agent; no cross-scope
  table ever holds another scope's plaintext.

---

## Honest status

`0.6.0`, **alpha.** All SPEC-041 phases have landed (zero-LLM capture, surface +
structural / analogical recall, the agentic sleep pass with signed memory tools +
pipeline fallback, nightly hygiene, search-before-write identity resolution, no-read-up
recall), and the subsystem is fully tested (adversarial fail-closed security tests
included). API may still shift.

Known limitations, stated plainly:

- **Structural / analogical recall is unproven.** It is architecturally real but has
  **no published benchmark** (LoCoMo / LongMemEval, etc.). Treat it as *different*, not
  yet *better*, until measured — a benchmark harness is the next release gate.
- **No managed hosting.** Local-first is a deliberate tradeoff; there is no autoscaled
  cloud option.
- **Young ecosystem.** No third-party integrations or track record at scale yet.

---

## License

To be set before public release.
