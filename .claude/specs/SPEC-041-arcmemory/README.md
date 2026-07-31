# SPEC-041 — arcmemory: dual-speed analogical memory

**Feature:** A new **`arcmemory`** package — Arc's memory substrate — replacing the two competing, half-wired memory backends inside `arcagent` (`bio_memory` + `memory`) with one clean system built on four ideas the SOTA agrees on and one idea that makes it *powerful*:

1. **Store distilled knowledge, not transcripts** — cheap deterministic capture on the hot path, richer facts distilled off it.
2. **Two speeds** — a *fast path* (every few turns, **zero LLM**) that appends a raw episodic stream + daily-log bullets and bumps entity/edge weights; a *slow path* (scheduled "sleep", **LLM-backed**) that connects, merges, decays, and rewrites the brain.
3. **Four memory types** — `episodic` (events), `semantic` (entities: people/places/projects), `procedural` (how-tos), and — the centerpiece — **`insight`** (patterns / theses / principles).
4. **Fused retrieval with recency + multi-hop + surrounding context** — BM25 + vector + graph, RRF-fused, decay-weighted.
5. **Analogical (structural) retrieval** — the hard part. Patterns and theses recur across situations with **near-zero lexical or semantic overlap** (the shared thing is deep *structure*, not words). `arcmemory` mints abstractions offline and matches the *current situation in abstraction space* — trigger-embedding similarity **plus** cue-graph spreading activation — then **enriches** the hit by traversing to its instances and neighbors. This is what turns a fast fact-cache into a system that *recognizes* a recurring situation and hands the agent the relevant past.

**Status:** PENDING
**Branch:** none (planning only — `.claude/` is gitignored)
**Type:** New package (`arcmemory`) + concern extraction from `arcagent` + one new retrieval paradigm
**Phase:** Phase 2 — SOTA + mission control (`ROADMAP-PROGRAM.md`)
**Supersedes:** the prior **SPEC-041 "close the learning loop"** (arcagent-modules-only wiring). That spec's threat mapping, classification-gating (`arctrust.dominates` no-read-up), forward-path-into-`sections` seam, and reflection→ACE loop are **retained and absorbed here**; its "keep memory inside arcagent modules" architecture is **replaced** by the `arcmemory` package extraction Josh selected 2026-07-07.
**Unblocks:** SPEC-044 (skill improver) and SPEC-047 (pluggable brain) — both list "memory" as their blocker.

---

## Why now (the trigger)

On 2026-07-07 the roadmap put SPEC-041 on hold with: *"Josh is rethinking memory architecture… Josh will SPECIFY the new memory architecture (his vision) → then spec+build."* This spec is that rethink. The prior investigation found the existing memory **known-rotten**: two redundant backends unaware of each other, a **dead forward path** (`ctx.data["memory_context"]` written, never read → recall silently dropped), and **`hybrid_search` is vaporware** (BM25-only; `sqlite-vec` imported nowhere; `embedding_model`/`search_weight_vector` dead config). This is the "producers-unwired" pattern — which is itself the motivating example for the `insight` layer below.

---

## Research base (four systems + one paper)

| Source | What we take |
|---|---|
| **FERNme** (Action-Coupled, Cost-Bounded Memory) | Zero-LLM deterministic writes; **spreading-activation** retrieval over a weighted graph; ACT-R decay + **salience-modulated forgetting**; **confidence-gates-action** (`known` acts silently / `guessed` verifies first); sanitize-untrusted-payload-before-memory; tamper-evident audit chain. |
| **mem0** | Store *extracted facts*, not turns; **single-pass** fused retrieval (vector+BM25+graph); scope IDs for isolation; **additive writes, conflicts resolved at read-time** (their team removed the write-path reconcile LLM loop — we heed that). |
| **AgentMemory** (rohitg00) | Four brain-inspired tiers; write pipeline **dedup → privacy-filter → store → distill → dual-index**; **hybrid-with-graceful-fallback** (BM25 always works, vector when available); nightly consolidation cron with Ebbinghaus decay + promotion. |
| **Hermes** (NousResearch) | **Glass-box capped markdown** injected as a **frozen prompt snapshot** (prefix-cache-friendly); hard capacity budget that **errors rather than auto-truncates** (forces curation); split of *always-in-prompt curated facts* vs *cheap searchable raw log*; add/replace/remove via unique-substring. |
| **Structure-mapping** (Gentner) — cog-sci | The analogical-retrieval framing: **surface features vs structural features**; you cannot retrieve a structure you never abstracted → abstraction must be a first-class, minted-offline step. |

---

## Concern-boundary split (the load-bearing decision)

| Concern | Owner | What it does | What it must NOT do |
|---|---|---|---|
| **Memory mechanics** (capture, 4 stores, fused + structural retrieval, decay/salience, consolidation orchestration) | **`arcmemory`** (NEW pkg) | Everything memory. Deterministic fast-path capture; the `Brain` implementation; distillation *driven by* arcllm | Run the agent loop; know about module-bus/hooks; write `identity.md`/`policy.md` |
| **Embed / distill text** | **`arcllm`** | Provider-agnostic `embed(texts)` + bounded structured completion for the nightly distill | Persist, index, rank, or schedule anything |
| **Persist raw stream + derived indices** | **arcmemory's own per-agent SQLite** (`workspace/memory/index.db`) | FTS5 keyword index; `sqlite-vec` vectors; cue-graph edges; decay/weight state — all **rebuildable**. `arcstore` cannot host these (closed 5-kind spool, no FTS5/vec); it receives only optional telemetry event rows | Curate; decide relevance; call an LLM |
| **Curated glass-box brain** | **filesystem (markdown)** | Daily-log bullets, entity/brain files, insight cards, procedural how-tos — human-editable, git-able, the **source of truth** for curated knowledge | Hold the high-volume raw stream (that's arcstore) |
| **Wire + schedule** | **`arcagent`** | Module-bus **hooks** call `Brain.capture()` / `Brain.retrieve()`; the **proactive engine schedules** `Brain.consolidate()`; enforces ACL (`memory_acl`) | Contain memory *logic* — it talks to memory only through the `Brain` Protocol |
| **Authorize recall** | **`arcagent/modules/memory_acl`** (kept) + **`arctrust`** | `SessionACL` visibility + classification no-read-up (`arctrust.dominates`) | Store or rank memory |
| **Pluggable brain extension** | **SPEC-047** | Generalize the `Brain` Protocol into a first-class config-selected extension point | Be hardwired |

**The subtle lines.** *Accumulating is not recalling* — value is the forward, query-conditioned, **analogically-matched** read. *The fast path never calls an LLM* (FERNme); the LLM lives only in the scheduled consolidation and an optional retrieval rerank. *`arcmemory` calls `arcllm`* the way `arcrun` does — that is not a concern violation, it is the sanctioned "all LLM calls go through arcllm." *arcagent holds no memory logic* — only the `Brain` Protocol seam, which is exactly what makes memory swappable ("replace with custom memory if desired"). *Reflection updates policy/context, never goals* — `identity.md` stays inode-locked (SPEC-035, ASI01).

---

## Dependency DAG placement

```
arctrust  (leaf)
   ▲   ▲   ▲
   │   │   └── arcstore ──┐
arcllm arcrun             │
   ▲   ▲                  │
   └─┬─┘                  │
     │                    │
  arcmemory ◀─────────────┘        (NEW: → arctrust, arcllm, arcstore)
     ▲
  arcagent   (→ arcmemory via Brain Protocol + hooks + scheduled task)
     ▲
  arccli / arcui / ...
```

`arcmemory` sits as a sibling to `arcrun`/`arcskill`, just below `arcagent`. It depends on `arctrust` (audit/policy/identity/sanitize), `arcllm` (embed + distill), `arcstore` (persist/index). It does **not** depend on `arcrun` unless consolidation later becomes a full agentic loop (SDD OQ-3 — default is a bounded structured `arcllm` call, no loop).

---

## Absorb / Delete inventory (WIRE-don't-rebuild · delete-as-you-add)

Existing memory is ~7,700 LOC across `arcagent/modules/{bio_memory,memory,memory_acl}`. It collapses into `arcmemory` + thin arcagent wiring — which also relieves the `arcagent/core` LOC budget.

**Absorb into `arcmemory` (the good bones):**
- `bio_memory/facts.py` — fact-triplet model (`predicate: value .confidence date | was:`) → `semantic` store.
- `bio_memory/entity_helpers.py` — `EntityIndex`, wiki-links → `semantic` graph.
- `bio_memory/` `Consolidator` + `DeepConsolidator` (the "sleep cycle" already exists, crash-safe manifest) → the `consolidate()` slow path.
- `bio_memory/` `Retriever` graph-traversal (1-hop today) → generalize to multi-hop enrichment.
- `memory/hybrid_search.py` FTS5 skeleton → keep FTS5, **add the real `sqlite-vec` layer** the old spec promised.
- `bio_memory` daily-notes + `working.md` → `episodic` + daily-log bullets.
- Security utils integration: `sanitize_text` (ASI06), `<memory-result>` boundary markers (LLM01), audit emission — **mandatory, non-negotiable**.

**Keep in place (thin wiring layer in `arcagent`):**
- `memory_acl/` wholesale — storage-agnostic ACL gatekeeper (`SessionACL`, signed `Capability` tokens, priority-10 veto). It gates the `Brain` calls.
- One `memory` tool surface (agent-facing recall/note) — **de-duplicated** (today two colliding `memory_search` tools).
- The module-bus hooks + proactive schedule that call the `Brain`.

**Delete in the same change (no-legacy):**
- Uninstantiated facade classes `MarkdownMemoryModule`, `BioMemoryModule`.
- One of the two duplicate `memory_search` tools / duplicate `agent:assemble_prompt@50` hook.
- Dead config `embedding_model`, `search_weight_vector`; the dead `ctx.data["memory_context"]` forward path (recall now writes into `ctx.data["sections"]`, which the assembler renders).
- The whole second backend once its parts are absorbed — no `bio_memory` **and** `memory` coexisting.

---

## Decisions (this session — 2026-07-07)

| # | Decision | Rationale |
|---|---|---|
| D-1 | **New `arcmemory` package**, not an arcagent module | Concern separation; scalable; makes the `Brain` a swappable extension point (SPEC-047). |
| D-2 | **Supersede SPEC-041** (reuse the slot) | 041 was the memory slot, on hold pending exactly this; SPEC-044/047 already depend on "memory". One spec, no zombie. |
| D-3 | **`arcmemory` owns consolidation, depends on `arcllm`; `arcstore` persists; `arcagent` schedules + hooks** | Memory logic cohesive in one package; LLM only off the hot path; arcagent stays thin → swappable brain. |
| D-4 | **Glass-box markdown (curated) + arcmemory's own per-agent SQLite (`workspace/memory/index.db`)** | Human-editable brain files as truth; per-agent SQLite for FTS5+vec+graph, rebuildable + hard-isolated. (Deepen DC-1: arcstore is a closed 5-kind spool, can't host FTS5/vec → arcmemory owns the index; arcstore gets optional telemetry only.) |
| D-7 | **Tier-varied decay/confidence constants** (SDD R-9 table) | Federal stricter: 4 hits to trust (γ=0.7), slower decay, higher τ + forget-floor. ADR-019 tier=stringency; bounds poisoning-promotion leverage at high tiers. |
| D-8 | **`bge-small-en-v1.5` default embedder** (ONNX, no PyTorch) | Better retrieval quality than MiniLM at ~same CPU cost — matters most for the analogical/trigger-embedding channel. MiniLM is the speed fallback. |
| D-9 | **Cross-encoder reranker (`bge-reranker`) in v1** | Personal OFF (deterministic score-margin fallback), enterprise/federal ON (top-20→top-5, scores audited at federal). Cross-encoder, NOT LLM-as-judge (cost/latency; forgeable-verdict risk). New `arcllm.rerank()` alongside `arcllm.embed()`. |
| D-10 | **arcmemory owns its own index DB** (not arcstore) | Confirms D-4/DC-1: hard per-agent isolation, air-gap-friendly, rebuildable; arcstore stays a clean telemetry spool. |
| D-5 | **Four memory types incl. `insight`; analogical retrieval is the centerpiece** | Keyword/vector are surface indices, blind to deep structure; patterns/theses need abstraction-space matching. |
| D-6 | **Fast path is deterministic (zero LLM); LLM only in scheduled consolidation + optional rerank** | FERNme cost/robustness; mem0's "no write-path LLM loop" lesson. |

---

## Open questions — status after `/deepen` (2026-07-07)

**Resolved by research (defaults set in SDD; no decision needed):**
- **OQ-1 ✅** — reuse the turn summary as the situation abstraction (MAC stage only needs recall-completeness) + **conjunctive two-channel gating** (trigger-embed AND cue-activation must agree) for precision. (SDD R-8)
- **OQ-2 ✅** — canonical-form lookup table + embedding-cluster merge of near-duplicate cues + size cap; ambiguous clusters queued not auto-merged. (SDD R-10)
- **OQ-3 ✅** — single **bounded, schema-constrained** arcllm call (reason→constrain), capped window, **event-count/idle-triggered** via the real `@background_task` interval poll (not cron); no agentic loop, no arcrun dep. (SDD R-10, DC-5)
- **OQ-7 ✅** — federal fail-closed `policy.pending` staging (diff-based, batchable); personal bounded auto-apply; curator stays **out of the agent's tool surface** (never feed the agent its own guard output). (SDD R-12)

**Resolved by Josh (2026-07-07) — all seven OQs now closed:**
- **OQ-STORAGE ✅** → arcmemory owns its own per-agent SQLite index (D-4/D-10); arcstore gets optional telemetry only.
- **OQ-4 ✅** → tier-varied constants (D-7); federal stricter per SDD R-9.
- **OQ-EMBED ✅** → `bge-small-en-v1.5` default, ONNX (D-8); MiniLM fallback.
- **OQ-RERANK ✅** → cross-encoder `bge-reranker` in v1 (D-9); personal OFF + margin fallback, enterprise/federal ON.

All open questions resolved. Spec is ready for `/implement` (Phase-0 scaffolding first).

---

## Success snapshot (what "done" looks like)

- One `arcmemory` package; `bio_memory` + `memory` deleted; `arcagent` talks to it only through `Brain`.
- Fast path adds a raw episodic event + daily-log bullet + entity/edge bumps with **zero LLM calls**, constant cost.
- A scheduled consolidation distills the day: new/updated facts, minted `insight` cards with triggers + cues, decayed stale edges — all audited, `identity.md` untouched.
- Retrieval returns a fused, decay-weighted, classification-gated bundle; a **planted analogical probe** (a new situation matching a past pattern with zero lexical overlap) **retrieves the pattern and its instances**.
- All security invariants from the prior SPEC-041 hold (no-read-up, boundary-marked-as-data, sanitized writes, audited recall, goal-lock).

## Deliverables

- `PRD.md` — EARS requirements, MoSCoW, pillar-tied acceptance criteria, threat mapping.
- `SDD.md` — package + DAG, four stores, dual-speed flows, the fused + **structural/analogical** retrieval design (the centerpiece), storage schema, the `Brain` Protocol, absorb/delete map, threat-surface map, **Research Insights** (`/deepen`).
- `PLAN.md` — TDD-ordered tasks, REQ→component→task traceable, tagged.
