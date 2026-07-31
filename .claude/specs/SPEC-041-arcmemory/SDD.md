# SDD — SPEC-041 arcmemory: dual-speed analogical memory

## 1. Design overview

`arcmemory` is a new package: one `Brain` behind a Protocol, four typed stores, two speeds, and a retriever with **two indices over one store** — a *surface* channel (the easy part) and a *structural* channel (the hard, differentiating part). `arcagent` talks to it only through the Protocol, via module-bus hooks and a scheduled task.

```
 ┌──────────────────────────── arcagent (wiring only) ────────────────────────────┐
 │  hook agent:pre_tool / post_respond ─► Brain.capture()      (fast, zero-LLM)    │
 │  hook agent:assemble_prompt         ─► Brain.retrieve()  ─► sections["recall"]  │
 │  proactive schedule (nightly/idle)  ─► Brain.consolidate()  (slow, LLM)         │
 │  memory tool (dedup'd) + memory_acl (no-read-up gate)                           │
 └───────────────────────────────────────┬────────────────────────────────────────┘
                                          │  Brain / Retriever Protocol (SPEC-047 seam)
 ┌────────────────────────────────────────▼───────────────── arcmemory ───────────┐
 │  FAST PATH  capture(event):  sanitize→privacy-filter→dedup                       │
 │     ├─ append raw episodic event ............................. arcstore          │
 │     ├─ append daily-log bullet ............................... file (glass-box)  │
 │     └─ tag entities + Hebbian edge bump (w←w+αm(1−w/W)) ....... arcstore graph   │
 │                                                                                  │
 │  SLOW PATH  consolidate():  read window's raw stream (LLM distill via arcllm)    │
 │     ├─ update semantic facts (additive, `was:` trail) ........ file + graph      │
 │     ├─ MINT insight cards {trigger, cues[], instances[]} ..... file + cue-graph  │
 │     ├─ promote repeated sequences → procedural cards ......... file              │
 │     └─ decay unreinforced edges (w·e^−λΔt, salience-slowed) ... arcstore          │
 │                                                                                  │
 │  RETRIEVE(situation):                                                            │
 │     surface   = RRF( vec(sqlite-vec) , bm25(FTS5) , graph ) · recency-weight     │
 │     structural= RRF( trigger-embed(situation) , cue-graph spreading-activation ) │
 │     fuse → confidence-gate(known/guessed) → no-read-up gate → ENRICH (multi-hop) │
 │            → boundary-marked, budgeted bundle                                     │
 │  Stores: episodic · semantic(entities) · procedural(how-to) · INSIGHT(patterns)  │
 └──────────────────────────────────────────────────────────────────────────────────┘
     depends on:  arctrust (audit/policy/sanitize) · arcllm (embed+distill) · arcstore
```

The novel machinery vs. today: the `arcmemory` package itself; a real `sqlite-vec` surface index (replacing the vaporware `hybrid_search`); the **`insight` store + structural retrieval channel** (entirely new); FERNme decay/salience/confidence; and a `Brain` Protocol seam. Reused unchanged: the ACE reflect/curate pipeline (`modules/policy`), the classification ladder + `dominates()`, the audit sinks, `memory_acl`, and the crash-safe consolidation manifest pattern.

## 2. Current state (what this supersedes) — confirmed by code read

All `file:line` in `packages/arcagent/src/arcagent`:
- **Fake hybrid search.** `modules/memory/hybrid_search.py:60-90` — `search()` calls `_bm25_search` (FTS5) only; no vector branch, no fusion. No `sqlite-vec` import anywhere; `_vec_available` (`:56`) is dead state; schema (`:148-175`) has no embedding table. `memory/config.py:24-28` — `embedding_model`, `search_weight_bm25/vector` have **zero readers repo-wide**.
- **Dead-wired forward path.** `modules/bio_memory/*` `agent:assemble_prompt` hook writes `ctx.data.setdefault("memory_context", ...)`, but the assembler (`core/session_internal/context.py:113-136`) reads only `ctx.data["sections"]`. Recall is silently dropped; and it is an unconditional working-memory dump, not query-conditioned.
- **Query never reaches assembly.** `core/session_internal/context.py:75-136` — `assemble_system_prompt` emits `agent:assemble_prompt` with no turn/goal text. Assembly runs **twice** per turn under spawn.
- **Lexical-only bio retrieval.** `modules/bio_memory/retriever.py` — `sum(content.count(term))` + substring boost + wiki-link 1-hop. No embeddings.
- **Redundant backends.** `bio_memory` and `memory` both register `memory_search` + `agent:assemble_prompt@50`, both write `workspace/entities/`, unaware of each other.
- **No embeddings in arcllm.** Only a hashing-trick `_embed` inside `modules/injection.py`. No `sqlite-vec`/`sentence-transformers`/`numpy` in deps.

Per no-legacy: both backends are deleted after their salvageable parts (see README absorb/delete inventory) move into `arcmemory`.

## 3. Package layout + DAG

```
packages/arcmemory/
├── pyproject.toml                 # deps: arctrust, arcllm, arcstore; extras: [vec]=sqlite-vec, [local]=sentence-transformers
├── src/arcmemory/
│   ├── __init__.py                # public: Brain, Retriever, MemoryConfig, types
│   ├── brain.py                   # Brain Protocol + DefaultBrain (orchestrates the 3 paths)
│   ├── config.py                  # MemoryConfig (Pydantic) — tiers, budgets, decay/salience constants
│   ├── types.py                   # Event, Fact, Entity, Insight, Procedure, Recall, Cue, Bundle
│   ├── capture.py                 # FAST PATH: deterministic capture (zero-LLM)
│   ├── consolidate.py             # SLOW PATH: distill, mint insights, decay (absorbs Consolidator/DeepConsolidator)
│   ├── distill.py                 # arcllm-backed structured extraction (fact + insight minting)
│   ├── stores/
│   │   ├── episodic.py            # raw stream (arcstore) + daily-log bullets (file)
│   │   ├── semantic.py            # entity graph + fact triplets (absorbs facts.py, entity_helpers.py)
│   │   ├── procedural.py          # how-to cards, promotion-by-repetition
│   │   └── insight.py             # pattern/thesis cards: {trigger, cues[], instances[]}
│   ├── index/
│   │   ├── surface.py             # sqlite-vec + FTS5 + graph, RRF fuse, recency weight
│   │   ├── structural.py          # trigger-embedding + cue-graph spreading activation
│   │   ├── graph.py               # weighted edges, Hebbian bump, decay/salience, spreading activation
│   │   └── rebuild.py             # re-derive every index from files + raw stream
│   ├── retrieve.py                # fuse surface+structural, confidence-gate, enrich (multi-hop)
│   ├── security.py                # sanitize, privacy-filter, boundary-markers, no-read-up gate
│   └── py.typed
└── tests/{unit,integration,security,performance}/
```

DAG: `arcmemory → {arctrust, arcllm, arcstore}`, sibling to `arcrun`/`arcskill`, below `arcagent`. **No** `arcmemory → arcagent`/`arcrun` import (TX.1 architecture test asserts this — REQ-001).

## 4. Components

### 4.1 `arcllm.embed` — embeddings capability (new, in arcllm)
```python
async def embed(texts: list[str], *, model: str) -> list[list[float]]: ...
class EmbeddingResponse(BaseModel): vectors: list[list[float]]; model: str; usage: Usage
```
Local default (`all-MiniLM-L6-v2`, 384-dim, via `[local]` extra), optional provider endpoint, rides SPEC-038 budget/rate/circuit-breaker + audit (LLM10). **Concern:** embeds only; persists/indexes/ranks nothing (REQ-041).

### 4.2 `Brain` — the Protocol + default (REQ-080)
```python
class Brain(Protocol):
    async def capture(self, event: Event, *, scope: Scope) -> None: ...              # fast, zero-LLM
    async def retrieve(self, situation: Situation, *, clearance: Classification,
                       top_k: int, budget: int) -> Bundle: ...                        # single-pass
    async def consolidate(self, window: TimeWindow, *, scope: Scope) -> ConsolidationResult: ...  # slow, LLM
    def rebuild_index(self, *, scope: Scope) -> None: ...                             # from files+stream
```
`DefaultBrain` wires capture.py / retrieve.py / consolidate.py over the stores + indices. `arcagent` depends only on `Brain` (config-selects impl) — the SPEC-047 seam.

### 4.3 Fast path — `capture.py` (REQ-010/011/012)
1. `security.sanitize(event.text)` (char allowlist, size cap, injection-pattern drop) + `privacy_filter` (strip secrets) + `dedup` (content hash, windowed).
2. `episodic.append(event)` → raw row in **arcstore**; `episodic.append_bullet(event)` → daily-log **markdown** line.
3. `semantic.tag_entities(event)` (controlled vocab + regex, **no LLM**) → for each co-active entity pair, `graph.hebbian_bump(a, b)` = `w ← w + α·m·(1−w/W)` (saturating, FERNme).
No LLM, no embedding. Constant-cost. Emits `memory.captured` audit event.

### 4.4 Slow path — `consolidate.py` + `distill.py` (REQ-030..034)
Scheduled by arcagent's proactive engine. Off the hot path; the **only** LLM use on the write side.
1. Read the window's raw stream from arcstore.
2. `distill.extract_facts(events)` → **arcllm** structured completion → new/updated `semantic` facts, **additive** with a `was:` trail on contradiction (resolved at read-time — REQ-032); confidence `1−e^−γ·hits` (REQ-033).
3. `distill.mint_insights(events, facts)` → **arcllm** → `Insight{id, statement, trigger, cues[], instances[], confidence, salience}` (§7). New insights start `guessed`.
4. `procedural.promote(events)` — action-sequences seen ≥ threshold become how-to cards.
5. `graph.decay()` — `w·e^−λΔt`, salience-slowed `λ_eff=λ(1−βs)`; edges below floor forgotten.
6. `index/*` reindex touched chunks; `stores/*` rewrite affected **markdown** files (atomic, crash-safe manifest — absorbed from `DeepConsolidator`).
Every mutation → `arctrust.audit.emit` (REQ-034). Additive + crash-safe (REQ-032, ASI06).

### 4.5 Retrieval — `retrieve.py` + `index/{surface,structural}.py` (REQ-040..043, 050..055)
Single-pass, bounded. See §6/§7 for the structural design in depth.
```python
async def retrieve(situation, *, clearance, top_k, budget) -> Bundle:
    surf = await surface.search(situation.text, top_k=K)              # vec+bm25+graph RRF, recency-weighted
    stru = await structural.match(situation, top_k=K)                 # trigger-embed + cue-graph spreading
    cand = rrf_fuse(surf, stru)                                       # REQ-040/051
    cand = confidence_gate(cand)                                      # known→actionable, guessed→"verify first" (REQ-053)
    cand = security.gate_no_read_up(cand, clearance, audit)          # drop over-clearance (REQ-060)
    bundle = enrich(cand, hops=cfg.hops)                             # multi-hop + surrounding context (REQ-042/052)
    return security.boundary_mark(truncate(bundle, top_k, budget))   # data-not-instructions, bounded (REQ-062/043)
```
Degrade: if embedder/`sqlite-vec` unavailable → surface = bm25+graph only, structural = cue-graph spreading only; emit `recall.degraded`; never raise (REQ-041).

### 4.6 arcagent wiring (thin — REQ-002)
- `modules/memory/` shrinks to: hooks (`capture` on `agent:pre_tool`/`post_respond`; `retrieve` on `agent:assemble_prompt@50` writing `sections["recall"]`, once-per-turn sentinel); the de-duplicated `memory` tool; and the proactive schedule entry calling `Brain.consolidate()`.
- `memory_acl/` kept wholesale (priority-10 veto, `SessionACL`, signed capability tokens).
- One core seam: `assemble_system_prompt(..., *, query)` threads the turn text into the `agent:assemble_prompt` payload (a few lines; core NCLOC unchanged-or-lower since ~7,700 LOC of logic leaves for arcmemory).

### 4.7 Reflection → ACE (retained from prior SPEC-041; feeds, does not replace)
`modules/policy/reflection.py` assembles grounded `ReflectionGrounding{episode_summary, step_results, failures}` from the consolidation episode and hands it to the **existing** `PolicyEngine._reflect`→`_curate` (REQ-070). Writes only bounded `policy.md`/`context.md` via the system Curator — never `identity.md`, never an agent tool (REQ-071, ASI01). Federal stages `policy.pending` (REQ-072). Every mutation audited.

## 5. Data & storage schema

**Glass-box files (source of truth, per-agent workspace):**
- `memory/daily-log/YYYY-MM-DD.md` — append-only bullets.
- `memory/entities/<slug>.md` — YAML frontmatter (`classification`, `cross_session_visibility`, confidence) + fact triplets `predicate: value .conf date | was: prior`.
- `memory/insights/<id>.md` — the pattern card (§7): statement, **trigger**, **cues**, **instances**, confidence, salience.
- `memory/procedures/<slug>.md` — how-to card, use-count.

**arcstore (raw stream + derived, rebuildable — REQ-021/022):**
- `episodic(event_id, ts, scope, kind, text, hash, refs)` — the high-volume stream + FTS5 mirror.
- `vec0(chunk_id, embedding float[D])` — sqlite-vec surface vectors.
- `fts_chunks(chunk_id, text)` — FTS5/BM25.
- `edges(src, dst, kind, weight, salience, last_hit, hits)` — semantic + **cue** graph (Hebbian/decay state).
- `chunks(chunk_id, source_path, mtime, classification)` — index provenance for rebuild + no-read-up label.

Everything in arcstore is disposable: `rebuild_index()` re-derives it from files + stream deterministically (AC-3).

## 6. Concern boundary (explicit)
- **arcmemory** owns all memory mechanics — capture, stores, both retrieval channels, decay/salience/confidence, consolidation orchestration. It **calls** arcllm (embed + distill) the way arcrun does; it never runs the agent loop and never imports arcagent.
- **arcllm** owns embed + the distillation completion. Persists/indexes/ranks nothing.
- **arcstore** owns the raw stream + derived indices. Curates/ranks/LLMs nothing.
- **arctrust** owns the classification decision (`dominates`/`parse_classification`), sanitize primitives, and audit. Reused, not reimplemented.
- **arcagent** owns wiring + scheduling only, through the `Brain` Protocol.
- **`modules/policy` (ACE)** owns curation; arcmemory feeds it grounded input. **arcskill (SPEC-044)** owns skill self-editing. Neither is reimplemented here.

## 7. The analogical (structural) retrieval design — the centerpiece

**Problem.** Patterns/theses recur across situations whose surface text and sentence-embeddings barely overlap; the match is deep *structure*. Example: the "producers-unwired" pattern recurred across SPEC-034/035/037/038/040 (budgets, trifecta, signing, planner) with near-zero lexical overlap. Keyword and vector are *surface* indices — constitutionally blind to it. **You cannot retrieve a structure you never abstracted** (Gentner structure-mapping).

**So abstraction is a first-class, minted-offline artifact.** When `consolidate.mint_insights` recognizes a pattern across a cluster of episodes, it writes an `Insight` with three things a raw episode lacks:
1. **`trigger`** — the situation stated at the *mechanism level*, surface stripped (e.g. "a security property is claimed, a predicate exists, but its producer is never traced → silent no-op"). This is **embedded** (via arcllm) into an *abstraction-space* vector, stored in a separate `insight_trigger` vec table (kept apart from surface chunk vectors so surface noise can't drown it).
2. **`cues[]`** — abstract feature tags (`claims-property`, `predicate-without-producer`, `mechanism-vs-activation`) drawn from a controlled, slowly-growing vocabulary. Each cue is a **node** in the graph; the insight links to its cues with weighted edges (strengthened Hebbian-style each time they co-fire at match time).
3. **`instances[]`** — links to the episodes it generalizes (the enrichment targets).

**Retrieval matches the current situation in abstraction space, two mutually-reinforcing ways (REQ-051):**
- **(a) trigger-embedding.** Form an abstracted description of the *current* situation — **default: reuse the turn's existing summary/context (OQ-1, no new LLM call)**; optionally, for high-value retrievals, a fresh bounded arcllm abstraction. Embed it; cosine-match against `insight_trigger` vectors. Once both sides are stated at the mechanism level, "budget breaker unreachable through the agent" and "trifecta gate has no leg-producers" land near each other — the abstraction **collapses the surface distance** that defeats raw embeddings.
- **(b) cue-graph spreading activation** (FERNme, over *abstract* cue nodes not words). The current situation lights up a few cue nodes (deterministic tagging + the summary's cues); activation flows over weighted edges (ACT-R base-level = recency × frequency, lateral inhibition, temporal decay) to insight nodes whose cues are active — retrieving matches with **zero surface overlap**. The graph edges *are* the learned "situation-shape → pattern" mapping.

RRF-fuse (a)+(b) with the surface channel. Then:
- **Confidence gate (REQ-053).** A `known` insight (recurred + corroborated, `1−e^−γ·hits` high) is an actionable anchor; a `guessed` insight (minted once) is surfaced **tentatively**, flagged *verify first*. Guessed insights that never re-match **decay out** (§4.4) — the store self-corrects, so a bad abstraction from the nightly LLM does not persist.
- **Enrich (REQ-052, "spot then enrich").** Traverse from the matched insight → its `instances` (episodes) + related entities + adjacent insights (bounded hops), plus surrounding raw-stream context around each episodic instance. The bundle is **anchored on the abstraction**, which is exactly why it surfaces things with no surface overlap to the query.
- **Optional LLM rerank (REQ-054).** Over the *small* structural candidate set only: "does the current situation truly instance any of these?" Off at personal, opt-in above. Precision booster, bounded, off the hot path.

**Cue-vocabulary hygiene (REQ-055):** consolidation periodically embedding-clusters cues and merges near-duplicates to bound drift (OQ-2).

Why this is still *simple*: the extra machinery is one more store (`insight`), one more index (`structural`), and a spreading-activation function over the graph we already keep. The complexity lives in the **offline** mint + a second index — **not** on the hot path. That's the whole trade: pay a little at night to make the day *powerful*.

## 8. Classification-gating design (retained detail)
- Bell-LaPadula two-label direction: `caller_clearance` (agent identity) vs `resource_classification` (memory `SessionACL.classification` frontmatter mapped onto the `arctrust.Classification` ladder). Gate = `dominates(caller_clearance, resource)`; drop on False (REQ-060).
- **Filter before assembly**, inside the recall path, so an over-clearance memory never enters `sections`, never affects visible rank/count, never appears in an error; the drop emits an audit event (REQ-061, AU-2).
- **Fail-closed at federal** (`parse_classification(strict=True)` on missing/unknown); personal warns and defaults `UNCLASSIFIED` (ADR-019).
- **No shared plaintext** (LLM08): per-agent index; classified plaintext never enters a cross-agent table (REQ-023).

## 9. Testing strategy (summary; full tasks in PLAN.md)
- **Unit:** zero-LLM capture (spy asserts no arcllm call); flat capture cost over 10k items; sanitize/privacy/dedup before write; consolidation mints an insight + promotes a procedure + decays stale / keeps salient edge + writes `was:` trail; semantic recall beats BM25-alone on a no-lexical-overlap fixture; RRF fusion; rebuild reproduces indices byte-identically; degrade-to-bm25+graph; no-read-up drop + audit; missing-label fail-closed at federal; boundary-marking + budget truncation; confidence gate flags guessed.
- **Integration:** query threaded through the **real** assembler reaches the hook once across spawn double-assembly and changes `sections["recall"]`; embeddings obtained only via arcllm (no provider import — architecture test); scheduled `consolidate()` runs via the proactive engine; reflection feeds the real `PolicyEngine`; audit chain verifies.
- **Security:** SECRET-into-UNCLASSIFIED blocked+audited; injection-laden memory doesn't alter behavior; reflection never writes/proposes `identity.md`, SPEC-035 tool-write denial still fires.
- **The differentiator test (AC-6):** a **planted structural probe** — a new situation matching a past `insight` with zero lexical/semantic overlap — is retrieved via BOTH trigger-embedding and cue-graph spreading, enriched with its instances; a never-recurring `guessed` insight decays out over simulated time.

---

## Research Insights (`/deepen`)

> Two threads: (A) techniques this spec implements — query-conditioned semantic retrieval, `sqlite-vec` recall, ACE curation, FERNme cheap-write/spreading-activation, and **analogical/structure-mapping retrieval**; and (B) Josh's standing directive — how **Hermes** self-improves, as adoption options with the arcskill-vs-arcagent split.

### R-1. Agentic Context Engineering (ACE) — arXiv:2510.04618 (Stanford/SambaNova/UC Berkeley, 2025)
Generator→Reflector→Curator over compact **delta bullets** merged by deterministic non-LLM logic (never full rewrite); grow-and-refine with embedding de-dup; +10.6% AppWorld. **Arc already implements this** in `modules/policy`. arcmemory does **not** rebuild it — it grounds the Generator's traces (REQ-070) and closes the automated-run gap (REQ-072). The paper's "context collapse" warning validates bounded-delta insistence (never let an LLM rewrite the whole playbook).

### R-2. Query-conditioned RAG — Lewis et al. NeurIPS 2020 (arXiv:2005.11401); "lost in the middle," Liu et al. TACL 2024 (arXiv:2307.03172)
Condition on top-k *relevance*, not recency; few well-placed items beat a large dump. Exactly the forward-path fix (REQ-040/043). Boundary-marking (REQ-062) mitigates RAG-injection (LLM01).

### R-3. sqlite-vec + on-device embedding — Alex Garcia `sqlite-vec` (2024); Sentence-BERT / MiniLM, Reimers & Gurevych EMNLP 2019 (arXiv:1908.10084); RRF, Cormack et al. SIGIR 2009
Zero-dependency in-process `vec0` cosine; MiniLM 384-dim local. Ideal for federal/air-gapped shared-nothing-per-agent (REQ-023): no network, deterministic, cold-start-cheap. **Adopt sqlite-vec + MiniLM-local default + RRF fusion + BM25-only fallback.**

### R-4. Reflexion — Shinn et al. NeurIPS 2023 (arXiv:2303.11366)
Reflect on a trajectory, store to improve later attempts. SPEC-040 deferred persisted reflective memory to here; we persist it into the ACE curated playbook (scored/pruned/bounded), not free-text — auditable, drift-resistant.

### R-5. FERNme — "Action-Coupled, Cost-Bounded Memory for Multi-Tenant Agents" (Sharipov, Acquilab, preprint v0.1.0)
- **Zero-LLM Hebbian write** over a fuzzy weighted graph (`w ← w + αm(1−w/9)`), retrieved by **spreading activation** (ACT-R base-level, lateral inhibition, temporal decay), compiled to a token-minimal card. **Salience-modulated forgetting** (`λ_eff=λ(1−βs)`) keeps a rare-but-significant one-shot signal above the forget threshold while neutral ones fade. **Confidence** `1−e^−γ·hits` distinguishes `known` (act silently) from `guessed` (verify first).
- **Security-by-construction:** event payloads are *untrusted* — tags sanitized (char allowlist, size caps, injection-pattern dropping) before becoming memory; every action in a tamper-evident audit chain; per-tenant isolation; glass-box editable card.
- **Arc adoption:** this is the backbone of the **fast path** (REQ-010/011 Hebbian bump), **decay/salience** (REQ-031), **confidence-gates-action** (REQ-033/053), **spreading activation** (the cue-graph channel, REQ-051b), and the **sanitize-before-memory + audit** invariants (REQ-012/034). We diverge from FERNme by adding a *distillation* slow path and the *insight/structural* layer FERNme (a preference graph) does not have — FERNme is cost-optimal but coarse; we spend a bounded nightly LLM to get abstraction.

### R-6. Analogical / structural retrieval — Gentner, "Structure-Mapping" (Cognitive Science, 1983); Gentner & Forbus MAC/FAC (1995); HippoRAG, Gutiérrez et al. NeurIPS 2024 (arXiv:2405.14831)
- **Surface vs structural similarity.** Human analogical retrieval is a two-stage MAC/FAC: a cheap, broad *surface* filter (MAC) proposes candidates, then a structural alignment (FAC) selects the true analog. Retrieval by deep structure requires an *abstracted representation* — surface features alone retrieve false friends and miss true analogs.
- **HippoRAG** shows a graph + personalized-PageRank spreading beats flat vector RAG on multi-hop association — the neurobiological "index" pattern (hippocampal index over neocortical detail).
- **Arc adoption — the centerpiece (§7):** mint the abstraction offline (`insight.trigger` + `cues`), then run a **MAC/FAC-shaped** retrieval: cheap surface + cue-spread propose candidates (MAC), the optional LLM rerank (REQ-054) does structural alignment (FAC) on the small set, and enrichment traverses the graph (HippoRAG-style spreading) to the instances. This is precisely the "spot the pattern with no surface overlap, then enrich with related knowledge/events" Josh specified.

### R-7. Hermes self-adaptation options (Josh's directive) — NousResearch/hermes-agent ("the agent that grows with you")
> **Project identified:** the harness Arc ported gateway/pairing/messaging patterns from is **Hermes Agent** (Nous Research), distinct from the Nous "Hermes" LLM line; loop = *solve → document → retrieve → improve → repeat*. Some specifics are from community write-ups — treat thresholds as indicative; ACE (R-1) is the rigorous backbone.
- **(a) Memory-first frozen snapshot + pluggable provider.** Two capped markdown files injected as a **frozen session snapshot** (protects prefix cache); genuine recall delegated to optional external providers (Mem0/Supermemory/Hindsight), one active at a time. **Arc option:** this is our `Brain`/`Retriever` seam (REQ-080, SPEC-047). **Adopt the seam, not the frozen-snapshot-only default** — Arc wants per-turn query-conditioned recall (R-2); mitigate the SPEC-029 prefix-cache tension by injecting recall in a **stable-ordered section** re-embedded only on query change.
- **(b) Background self-improvement review after complex tasks** (`ReflectionFrame`: failure classification, tool insights, world-model updates). **Arc option:** our grounded `ReflectionStep` (§4.7) — but routed into a **scored/pruned/capped** ACE playbook, closing the automated-run gap. Adopt the *trigger*, reject the *unstructured store*.
- **(c) Skills as a self-patched living playbook** — belongs to **arcskill/SPEC-044**, not here. Policy bullets change *behavior*; skills change *capabilities*. Keep separate; both optional core extensions via SPEC-047.
- **(d) Safety cautionary case (`skills_guard.py`)** — reportedly off-by-default, fails-open, and in `ask` mode **returns scan findings to the agent** (lets it iteratively evade), regex misses aliasing. **Arc lesson (informs REQ-071/072, OQ-4/5):** bounded ACE playbook (no unbounded self-rewrite) + inode-locked `identity.md` (SPEC-035) + federal `policy.pending` staging + **never hand the agent its own guard findings** + deterministic non-agent-tool curator. Hermes' defects are the argument *for* Arc's fail-closed bounded curation.

### Synthesis
**Fast path = FERNme (zero-LLM Hebbian write, decay/salience/confidence, sanitize+audit).** **Surface recall = sqlite-vec + MiniLM-local + RRF + BM25 fallback (R-3), query-conditioned (R-2).** **Structural recall = minted abstraction + trigger-embed + cue-graph spreading, MAC/FAC-shaped, HippoRAG-style enrichment (R-6) — the differentiator.** **Reflection = Reflexion-grounded (R-4) into the existing ACE curator (R-1), bounded/audited.** From Hermes (R-7) adopt the pluggable-brain seam and the reflect-after-substantive-run trigger; reject frozen-snapshot-only recall, the unstructured store, and the findings-leaking self-mod guard. Skill self-editing stays in arcskill/SPEC-044; the seams generalize in SPEC-047.

---

## Research Insights (Deepen — 2026-07-07)

Six parallel research streams (analogical mechanics, memory-dynamics constants, consolidation, sqlite-vec scaling, rerank/HITL, and **in-repo seam verification**). The seam verification is load-bearing: it caught contradictions between this SDD's assumptions and the actual codebase — those are corrected first, because leaving them is the "producers-unwired" trap ([[feedback_producers_unwired_pattern]]).

### DC — Seam-reality corrections (from in-repo recon; each is a real API `file:line`)

- **DC-1 (load-bearing — overrides D-4/REQ-021).** **`arcstore` CANNOT host the memory index.** `arcstore` is a *closed 5-kind operational spool* (`llm_call|run_event|agent_event|tool_event|spawn_event`, `records.py:17`), fixed SQLite DDL per kind, **no FTS5, no sqlite-vec, no virtual tables, no per-scope store object** (`backends/sqlite.py`, `backends/base.py:18` `OPERATIONAL_TABLES` allowlist), one shared path `~/.arc/store` (`config.py:26`). → **arcmemory owns its own per-agent SQLite** (`workspace/memory/index.db`: FTS5 + `vec0` + `edges`), exactly as the old `bio_memory` used a workspace `search.db`. `arcstore`'s role narrows to **optional telemetry emission** — arcmemory may `record()` an `agent_event` spool row per capture/consolidation for the operational plane, but the *index* is arcmemory-owned and rebuildable. Storage split becomes: **glass-box markdown (truth) + arcmemory per-agent SQLite (index, disposable) + optional arcstore event spool (telemetry)**. *(pending Josh confirm — see OQ-STORAGE)*
- **DC-2.** **`sanitize_text` is NOT in arctrust** — it lives in `arcagent/utils/sanitizer.py:42`. arcmemory must **not** import arcagent (DAG). → `arcmemory/security.py` owns its **own** sanitizer (absorb the ~1 helper). Correct the README concern table: sanitize is arcmemory's, not arctrust's.
- **DC-3.** **`audit.emit(event, sink)` requires an explicit sink** (`arctrust/audit.py:494`) — no global emitter. → arcmemory injects an `AuditSink` (e.g. `WormSink`, `audit.py:170`); `AuditEvent` fields are `{actor_did, action, target, outcome, classification, tier, request_id, payload_hash, ts, extra}`.
- **DC-4.** **Hook seam names/shape differ.** Context is **`EventContext`** (`module_bus.py:30`) not `HookContext`; events are **`agent:`-prefixed**; **there is no `post_respond`** — it's **`agent:pre_respond`** (`agent_dispatch.py:111`). Capture hooks → `agent:post_tool` (`tool_registry.py:503`) + `agent:pre_respond` / `agent:shutdown`. Retrieval hook → `agent:assemble_prompt` @ priority 50. `@hook(*, event, priority=100, tryfirst, trylast)` (`_decorator.py:176`).
- **DC-5.** **Background seam is interval-seconds, not cron.** `@background_task(*, interval: float, name=None)` (`tools/_decorator.py:214`) polls every `interval` seconds (drain-then-replace on reload); cron cadence exists only in `SchedulerEngine` (user-NL-scheduled agent runs, a different seam). → consolidation is **threshold-triggered** (event-count / idle), implemented as a cheap `@background_task` poll that checks the trigger — which the research (R-10) says is *better* than a fixed nightly cron anyway. `arcllm.embed()` confirmed **absent** today (only the hashing `_embed` in `modules/injection.py`) → Phase-1 build is required (OQ-6 = yes). `arcteam.TeamMemoryService` **exists** (`arcteam/memory/service.py:38`: `search/promote/record_decision/rebuild_index`) → the optional team-share is orchestrated by **arcagent** (which depends on both), not by arcmemory directly (keeps arcmemory's deps to arctrust+arcllm+its-own-SQLite).

### R-8. Analogical mechanics — concrete parameters (deepens §7)
- **Spreading activation:** ACT-R `A_i = B_i + Σ_k W_k·S_ji`, spread strength `S_ji = S − ln(fan_j)` (the **fan effect**: a cue with high out-degree contributes *less* — a **built-in precision/anti-poison control**; `S≈1.45–2.0`). Traverse **hop-capped (2–3)**, attenuate by `1/fan` at each source, stop when cumulative activation `< τ`. Pure graph arithmetic, **zero LLM**, O(edges touched) → fine at 100k nodes with a hop cap.
- **HippoRAG vs plain spreading:** Personalized PageRank (damping 0.5–0.85) is a *global, converged, deterministic* multi-hop score; ACT-R spreading is *local, hop-limited, cheaper*. **Adopt hop-capped spreading for v1** (boring, in-process, naturally bounded); PPR is the upgrade path if multi-hop recall proves insufficient. HippoRAG2's LLM triple-filter = our optional rerank (DC/OQ-5).
- **MAC/FAC mapping (confirmed):** cheap surface + cue-spread = **MAC** (must be *recall*-complete, not precision-complete — a lossy monotonic filter is enough); optional LLM/cross-encoder rerank = **FAC** on the small pool only.
- **Cold start (real risk):** structural retrieval is a *learned overlay* — empty until insights accrue; falls back to surface. Mitigate with an **explicit minimum-insight-count trigger + periodic backfill**, not implicit hope (else the structural channel stays perpetually empty).
- **False-positive control:** **require BOTH channels to agree** (trigger-cosine AND cue-activation over threshold) before promoting a structural candidate — conjunctive gating is the boring, standard fix; LLM check is final-arbiter on the filtered set only.

### R-9. Memory-dynamics constants (answers OQ-4) — recommended default table
| Const | Personal | Enterprise | Federal | Note |
|---|---|---|---|---|
| α write-rate | 0.3 | 0.2 | 0.15 | slower where poisoning risk higher |
| W saturation | 1.0 | 1.0 | 1.0 | normalized cap |
| λ_fast | 0.15/day (~5-day half-life) | " | " | recent-context edge |
| λ_slow | 0.01/day (~70-day) | 0.01 | 0.008/day | durable edge; **fast 5–20× slow** |
| β salience-damping | 0.6 | 0.6 | 0.5 | federal caps spoof leverage |
| γ confidence | **0.536** (3 hits→0.8) | 0.536 | 0.7 (~4 hits) | `1−e^(−γ·hits)` |
| ACT-R d | 0.5 | 0.5 | 0.5 | literature standard |
| τ retrieval threshold | 3.5 | 4.0 | 4.5 | federal: higher bar to surface |
| forget floor | 0.02 | 0.02 | 0.05 | federal never fully forgets (AU immutability) |
- **Decay is O(1) per lookup, not a cron sweep:** store `(w0, t0, λ_eff)` and evaluate `w0·e^(−λ_eff·Δt)` **on read** — turns 100k-edge decay from an O(n) nightly job into O(1) per access. *(scalability)*
- **Salience without an LLM:** `s = clamp(w1·|z-score of outcome| + w2·rating-extremity + w3·explicit-flag, 0, 1)` from signals the audit trail already logs (task success/fail flip, error raised, "important"/caps/repeat, correction). *(zero extra LLM)*
- **Poisoning defense (γ/β are the attack surface):** count hits from **distinct sessions/callers** (not repeats within one session), **rate-limit** hit-counting per `caller_did`, and cap β so even max fake salience can't fully halt decay. *(security)*

### R-10. Consolidation (answers OQ-3) — single bounded call, threshold-triggered
- **Generative Agents template:** importance-triggered reflection (fires when summed importance crosses ~150, ~2–3×/day) as a **bounded two-step single-purpose call** — "3 salient questions → retrieve → synthesize insights **with source-event citations**." Not an agentic loop. Adopt: mint insights with **instance-ID citations** (audit trail + grounding).
- **mem0's lesson (confirmed):** they *removed* the write-time ADD/UPDATE/DELETE LLM router → **additive-only single-pass extraction, conflicts resolved at read-time** (LoCoMo 71→92, p95 17s→1.4s). arcmemory is additive with `was:` trails (REQ-032).
- **Reliability:** **reason-free-form → schema-constrained** two-step (constrained decoding / tool-forcing) beats hoping for JSON (+13% accuracy for ~12% tokens); **cap the window** (AWS AgentCore `historicalContextWindowSize`) and **chunk** when it exceeds the model budget — a single call degrades once the batch exceeds reliable attention.
- **Cadence:** **event-count / idle-triggered** (AWS, Generative Agents) beats fixed nightly; implement via the `@background_task` poll (DC-5). Window is capped, not total-history → **bounded per-run cost** (scalability).
- **Cue governance (answers OQ-2):** canonical-form lookup table + **embedding-cluster merge** of near-duplicates (CESI/MulCanon) + size cap; ambiguous clusters queued, not auto-merged.

### R-11. sqlite-vec scaling + embeddings (answers OQ-6) — explicit ceilings
- **Brute-force cosine, no ANN yet.** Latency ~**0.5 ms @ 10k**, **6–75 ms @ 100k** (384-dim well under), "a few seconds" @ 1M×1024-dim. **Per-agent lifetime memory is low tens-of-thousands of chunks → sub-20 ms, brute force is correct; no ANN needed.** Partition-key columns shrink the scan on natural partitions (date/kind). Per-agent DB = hard isolation, bounds every scan regardless of fleet size. *(scalability ceiling: ~50k chunks/agent comfortable; >100k → add partition keys)*
- **Embedder:** `all-MiniLM-L6-v2` (384-dim, ~90 MB) is the CPU-speed leader (~14k sent/s) but lowest quality (~56% top-5); **`bge-small-en-v1.5` / `gte-small`** give a real quality bump at similar CPU cost (<30 ms) — **recommended default is bge-small**, MiniLM the speed fallback. **ONNX backend removes the PyTorch dependency** (3–5× faster, 60–80% less memory); Candle (Rust) is the extreme-air-gap path. *(→ OQ-EMBED)*
- **Incremental:** two-level hashing (doc SHA-256 gates → chunk hash gates re-embed) → 80–90% cache hit on re-index. Store `content_hash, version, indexed_at`.
- **RRF:** `Σ 1/(k+rank_i)`, **k=60**; weighted-RRF when one retriever is better-calibrated; **add recency as a THIRD ranked list**, not by mutating the fused score (preserves RRF's scale-free property). *(corrects REQ-040's "recency-weight" — recency is a fused rank, not a multiplier)*
- **Federal:** embedder runs **in-enclave** (remote embeddings API = the #1 air-gap break); model weights need **AI-BOM provenance** (checksum + manifest, OWASP AI-BOM 2025) verified at load like any signed artifact (ADR-019). *(security)*

### R-12. Rerank + HITL staging (answers OQ-5, OQ-7)
- **Rerank = cross-encoder, NOT LLM-as-judge** (`bge-reranker-v2-m3`, 278M, ~20–50 ms CPU for <100 pairs; LLM rerank is an order of magnitude costlier). Pattern: **top-20 → top-5**; beyond ~50 candidates adds latency without recall.
- **Tier default:** **personal = OFF** with a **deterministic score-margin fallback** (rerank only when rank-1/rank-2 margin < threshold — ~19× speedup at parity); **enterprise/federal = cross-encoder always-on**, scores **audited at federal**. *(→ OQ-RERANK)*
- **HITL staging (NIST AI RMF / EU AI Act Art. 14):** risk-based routing — human approval only for *consequential, rare* writes (a new policy bullet), **diff-based** (old vs proposed), **batchable** (queue + review window, not per-event interrupts) to avoid approval fatigue. Federal = fail-closed (bullet inert until approved); personal = bounded auto-apply under a confidence/impact threshold.
- **Never feed the agent its own guard output (corroborated):** OpenAI's guardrail judge was forged when its confidence score re-entered the agent's context. **The curator/approval queue must sit OUTSIDE the agent's tool registry** — the agent proposes, but cannot read/edit/query the pending state. Reinforces REQ-071 (curator is deterministic, not an agent tool).

### R-13. Net effect on the design
The corrections tighten, not expand, the design: arcmemory owns a **single per-agent SQLite** (DC-1) rather than leaning on arcstore; consolidation is **event-count-triggered via the real `@background_task` interval poll** (DC-5, R-10); recency joins the fusion as a **third RRF list** (R-11); rerank is a **cross-encoder, tier-gated with a deterministic margin fallback** (R-12); decay is **evaluated on read** (R-9, O(1)); structural retrieval uses **hop-capped fan-attenuated spreading + conjunctive two-channel gating** with an explicit **cold-start backfill** (R-8). All still boring, in-process, deterministic where it counts, LLM only at the bounded nightly distill + optional cross-encoder rerank.
