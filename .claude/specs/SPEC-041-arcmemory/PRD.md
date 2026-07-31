# PRD — SPEC-041 arcmemory: dual-speed analogical memory

## Product context

- **Steering:** `.claude/specs/ROADMAP-PROGRAM.md` → Phase 2 (SOTA + mission control). Supersedes the prior SPEC-041 (learning-loop-in-arcagent-modules). Unblocks SPEC-044 (skill improver) + SPEC-047 (pluggable brain).
- **Problem:** Arc's memory is two competing half-wired backends (`bio_memory` + `memory`) with a dead forward path and a vaporware `hybrid_search`. It *accumulates* but does not *recall the right thing at the right time*, and it has **no way to surface a recurring pattern/thesis** whose match to the present is structural, not lexical. Memory logic also lives tangled inside `arcagent`, so it can neither scale cleanly nor be swapped.
- **Outcome:** A standalone **`arcmemory`** package that (a) captures cheaply and deterministically on the hot path (zero LLM); (b) distills a **glass-box brain** (four stores: episodic / semantic / procedural / **insight**) on a scheduled "sleep" pass; (c) retrieves a fused, decay-weighted, classification-gated, multi-hop-enriched bundle; and — the differentiator — (d) performs **analogical (structural) retrieval**: it recognizes when the current situation instances a past pattern with near-zero surface overlap, and enriches that hit with its instances and neighbors. `arcagent` talks to it only through a `Brain` Protocol (swappable) via hooks + a scheduled task.
- **Non-goals (this cycle):** (a) **skill** self-improvement — SPEC-044 (arcskill); (b) generalizing the `Brain` seam into the first-class extension-point *framework* — SPEC-047 (this spec *defines and ships* the seam, SPEC-047 generalizes it); (c) a NATS/cross-node distributed memory fabric — Phase 4; (d) reimplementing ACE curation — `arcmemory` *feeds* the existing `modules/policy` Curator, it does not replace it; (e) a bespoke LLM client — embedding + distillation go through **arcllm**.

## Pillars (priority order per CLAUDE.md)

1. **Simplicity** — one package, one `Brain`, four stores, two speeds. Deterministic fast path (no LLM). Glass-box files a human can read. Delete both old backends in the same change (no-legacy).
2. **Modularity** — `arcmemory` owns mechanics behind a `Brain` Protocol; `arcllm` embeds/distills; `arcstore` persists/indexes; `arctrust` audits/authorizes; `arcagent` only wires + schedules. Swapping the brain is a Protocol swap, not a rewrite (SPEC-047 seam).
3. **Security** — untrusted content sanitized before it becomes memory (ASI06); recall classification-gated no-read-up (LLM02); injected as boundary-marked data never instructions (LLM01); per-agent isolation (LLM08); bounded retrieval/embedding (LLM10); reflection cannot touch goals (ASI01); every mutation + recall audited (AU-2/9/10).
4. **Scalability** — shared-nothing per agent; fast path is O(1) constant-cost, zero-LLM; retrieval top-k + budget-bounded; consolidation batched off the hot path; raw stream + indices in `arcstore` (scales past markdown); cold-start + <50 MB baseline preserved.

---

## Requirements (EARS + MoSCoW) — **M**ust / **S**hould / **C**ould / **W**on't

### A. Package + concern extraction

- **REQ-001 (M).** The system **shall** ship a new installable `arcmemory` package (src layout, `py.typed`, own tests) depending only on `arctrust`, `arcllm`, `arcstore` — **not** on `arcagent` or `arcrun`. Architecture regression tests **shall** confirm no `arcmemory → arcagent` import. *(Modularity)*
- **REQ-002 (M).** All memory *logic* **shall** live in `arcmemory`; `arcagent` **shall** retain only wiring — module-bus hooks that call the `Brain`, a scheduled consolidation task, the de-duplicated agent-facing `memory` tool, and `memory_acl`. `arcagent/core` **shall** gain **zero** memory logic. *(Simplicity)*
- **REQ-003 (M).** The two existing backends (`modules/bio_memory`, `modules/memory`) and their dead artifacts (facade classes `MarkdownMemoryModule`/`BioMemoryModule`, duplicate `memory_search` tool, dead `embedding_model`/`search_weight_vector` config, dead `ctx.data["memory_context"]` path) **shall** be **deleted in the same change** once their salvageable parts are absorbed. No two backends coexist. *(Simplicity — no-legacy)*

### B. Fast path — deterministic capture (zero LLM)

- **REQ-010 (M).** On a capture trigger (module-bus hook, every N turns / on significant events), the system **shall** append a raw `episodic` event and a daily-log bullet **without any LLM call**. Capture cost **shall** be constant-time regardless of store size. *(Simplicity, Scalability)*
- **REQ-011 (M).** Capture **shall** deterministically tag entities (controlled vocabulary + regex) and apply a saturating **Hebbian** weight bump to their graph edges (`w ← w + α·m·(1−w/W)`), strengthening co-active pairs — no LLM, no embedding on the hot path. *(Scalability)*
- **REQ-012 (M).** Before any content becomes memory it **shall** be **sanitized** (character allowlist, size caps, injection-pattern dropping) and **privacy-filtered** (strip secrets/keys) and **deduplicated** (content hash within a window). *(Security — ASI06/LLM01)*

### C. Four stores + storage substrate

- **REQ-020 (M).** The system **shall** maintain four typed stores: **`episodic`** (timestamped events, decays), **`semantic`** (entities: people/places/projects, as a weighted graph of fact-triplets), **`procedural`** (how-to cards, promoted by repetition), **`insight`** (patterns/theses/principles, minted by distillation — §F). *(Simplicity)*
- **REQ-021 (M).** The **curated** layer (daily-log bullets, entity/brain files, procedural cards, insight cards) **shall** be human-editable markdown files on disk — the **source of truth** — while the **raw episodic stream and all derived indices** (FTS5, vector, cue-graph, weights/decay state) **shall** live in **arcmemory's own per-agent SQLite** (`workspace/memory/index.db`). *(Deepen DC-1: `arcstore` is a closed 5-kind spool with no FTS5/vec and no per-scope store — it cannot host the index; arcmemory owns it, and may emit optional telemetry event rows to `arcstore`.)* *(Simplicity, Scalability)*
- **REQ-022 (M).** Every derived index **shall** be **fully rebuildable** from the curated files + raw stream (disposable index; files are truth). A rebuild command **shall** re-derive FTS5 + vectors + cue-graph deterministically. *(Security — LLM08; Scalability)*
- **REQ-023 (M).** Stores **shall** be **per-agent, shared-nothing** (scope IDs: agent DID / session); no cross-agent shared table holds another agent's plaintext. Optional explicit team-shared entities **shall** go through an explicit, audited share, never implicit bleed. *(Security — LLM08, ASI03)*

### D. Slow path — scheduled consolidation ("sleep")

- **REQ-030 (M).** `arcagent`'s proactive engine **shall** be able to **schedule** `Brain.consolidate()` (e.g. nightly / idle). Consolidation **shall** run off the hot path and **shall** be the *only* place the fast-capture data is distilled by an LLM. *(Scalability)*
- **REQ-031 (M).** Consolidation **shall** read the window's raw episodic stream and: extract/update `semantic` facts; **mint/refresh `insight` cards** (§F); promote repeated action-sequences to `procedural` cards; apply **decay** to unreinforced edges (`w·e^−λΔt`) with **salience**-slowed forgetting (`λ_eff = λ(1−βs)`); and rewrite the affected curated files. *(Simplicity)*
- **REQ-032 (M).** Consolidation **shall** be **additive-biased** — new facts accumulate; contradictions are recorded with a `was:` trail and resolved at **read time** by recency/confidence, **not** by destructive write-time DELETE (mem0's lesson). Merges/edits **shall** be crash-safe (manifest/atomic write, as the existing `DeepConsolidator` already does). *(Security — ASI06)*
- **REQ-033 (M).** Each memory carries a **confidence** that grows with corroboration (`1−e^−γ·hits`); an item seen once is `guessed`, a recurring/corroborated one becomes `known`. *(Security — LLM09)*
- **REQ-034 (M).** Every consolidation mutation (facts added/updated, insights minted/promoted/decayed, files rewritten) **shall** emit an `AuditEvent` through `arctrust.audit.emit`. *(Security — AU-2/9/10)*

### E. Fused retrieval (surface + recency + multi-hop)

- **REQ-040 (M).** Retrieval **shall** fuse **vector** (`sqlite-vec` cosine), **BM25** (FTS5 keyword), and **graph** signals via **Reciprocal Rank Fusion** (k=60), with **recency added as a third ranked list** in the fusion (not as a score multiplier — preserves RRF's scale-free property; Deepen R-11). Decay is evaluated **on read** from stored `(w0, t0, λ_eff)` — O(1), no cron sweep (R-9). *(Simplicity, Scalability)*
- **REQ-041 (M).** All embedding inference **shall** route **through arcllm**; `arcmemory` **shall not** import a provider adapter. When no embedder is available the system **shall** degrade gracefully to **BM25 + graph** and surface (audit) the degraded mode — never fail. *(Modularity; Scalability — ADR-019)*
- **REQ-042 (M).** Retrieval **shall** support **multi-hop enrichment** and **surrounding context**: a hit returns not only the matched item but its graph neighbors (N-hop, bounded) and, for an episodic hit, the adjacent raw-stream events around it. *(Simplicity — the "grab related + surrounding context" ask)*
- **REQ-043 (M).** Retrieval **shall** be **single-pass** (no agentic loop) and **bounded**: top-k + configurable token budget; over-budget truncates lowest-ranked first, never overflows the prompt. *(Security — LLM10; Scalability)*

### F. Structural / analogical retrieval — the centerpiece

- **REQ-050 (M).** When it mints an `insight` (pattern/thesis/principle), consolidation **shall** also produce, for that insight: an **abstracted trigger** statement (surface-stripped, mechanism-level), a set of **cue tags** drawn from a controlled-but-growing vocabulary (each cue is a graph node), and **links to the instances** it generalizes. *(Simplicity — you cannot retrieve a structure you never abstracted)*
- **REQ-051 (M).** Retrieval **shall** include a **structural** channel that matches the *current situation in abstraction space*, by BOTH: (a) **trigger-embedding similarity** — embed an abstracted description of the current situation (default: reuse the turn's existing summary; no new LLM call) and match it against insight *trigger* embeddings; and (b) **cue-graph spreading activation** — activate cue nodes implied by the current situation and flow activation (FERNme-style, ACT-R base-level) to insight nodes whose cues are active — retrieving matches with **zero lexical/semantic surface overlap**. *(Simplicity — the differentiator)*
- **REQ-052 (M).** A structural hit **shall** be **enriched** before return: traverse from the matched insight to its instances (episodes), related entities, and adjacent insights, assembling a bundle anchored on the abstraction. *(Simplicity — "spot, then enrich")*
- **REQ-053 (M).** Insights **shall** be **confidence-gated on action** (REQ-033): a `guessed` insight is surfaced **tentatively** (flagged "verify first"); a `known` insight may be surfaced as an actionable anchor. A `guessed` insight that never recurs **shall** decay out. *(Security — LLM09; self-correcting)*
- **REQ-054 (M).** The system **shall** provide a **cross-encoder rerank** (`bge-reranker` via a new `arcllm.rerank()`, NOT an LLM-as-judge) over the *small* candidate set (top-20→top-5). It **shall** be tier-gated: **personal OFF** with a deterministic **score-margin fallback** (rerank only when top-1/top-2 margin < threshold); **enterprise/federal ON**, scores **audited at federal**. The reranker/curator **shall never** be exposed in the agent's tool surface (its verdict must not re-enter the agent's context — Deepen R-12). *(Security — LLM09/LLM10 bounded; precision)*
- **REQ-055 (S).** Consolidation **shall** periodically **dedup/merge near-duplicate cues** (embedding-cluster) to bound cue-vocabulary drift. *(Simplicity)*

### G. Security — classification-gated recall (retained from prior SPEC-041)

- **REQ-060 (M).** Before injecting a retrieved memory, the system **shall** compare its classification label to the caller/agent clearance via `arctrust.dominates(clearance, resource)` and **drop** any the clearance does not dominate (no-read-up). A missing/unknown label **shall** fail closed at federal (`strict`); personal defaults `UNCLASSIFIED` with warn. *(Security — LLM02, AC-3)*
- **REQ-061 (M).** A dropped over-clearance memory **shall not** leak via rank, count, or error; the drop **shall** be audited. *(Security — LLM02, AU-2)*
- **REQ-062 (M).** Injected recall **shall** be wrapped in `<memory-result>` boundary markers (source + score + confidence) and framed as **data, not instructions**. *(Security — LLM01)*

### H. Reflection → behavior change (retained, condensed)

- **REQ-070 (M).** After a completed run, a reflection step **shall** feed grounded experience (the consolidation episode + real step results) to the **existing** `modules/policy` ACE Curator — no second curation algorithm. Ungrounded/empty reflection writes nothing. *(Simplicity; Security — LLM09)*
- **REQ-071 (M).** Reflection **shall** update only curated `policy.md`/`context.md` bullets (bounded: score-clamp `[1,10]`, prune, cap, sanitize) via the system Curator; it **shall not** write/propose `identity.md`, and **shall not** be an agent-callable free-write tool. SPEC-035 goal-lock stays in force. *(Security — ASI01, ASI06)*
- **REQ-072 (S).** Session-less automated runs **shall** still produce bounded grounded reflection (close the learning gap). Whether a bullet auto-applies or stages as `policy.pending` **shall** be config/tier-controlled (federal stages for operator review). *(Security — ADR-019)*

### I. Extensibility, tier, budget

- **REQ-080 (M).** `arcmemory` **shall** expose a `Brain` Protocol (`capture`, `retrieve`, `consolidate`, `rebuild_index`) and a `Retriever` Protocol (surface + structural channels pluggable/stackable). `arcagent` **shall** depend only on these Protocols, config-selecting the implementation (default `arcmemory`). *(Modularity — SPEC-047 seam)*
- **REQ-081 (M).** The system **shall** function at every tier; tier changes stringency only (federal: local/on-prem embedder, strict classification, `policy.pending` gate, all mutations audited). *(Security — ADR-019)*
- **REQ-082 (M).** Embedding + distillation calls **shall** ride the existing SPEC-038 budget/rate/circuit-breaker ceilings. *(Security — LLM10)*
- **REQ-083 (C).** Memory state (captures, consolidations, recalls, insight mints) **could** surface to `arcui` via the existing audit fan-out (UIBridgeSink) — no new push wire. *(Modularity)*

---

## Threat mapping

| Threat | Requirement(s) | Mitigation |
|---|---|---|
| **ASI06 — Memory/context poisoning** | REQ-012, REQ-022, REQ-032, REQ-071 | Sanitize before write; index re-derivable from truth; additive + crash-safe consolidation with `was:` trails (no destructive rewrite); curated bullets bounded/clamped/pruned/sanitized. |
| **LLM01 — Prompt injection** | REQ-012, REQ-062 | Injection-pattern dropping at capture; recall wrapped as boundary-marked data, never instructions. |
| **LLM02 — Sensitive disclosure** | REQ-060, REQ-061 | `arctrust.dominates` no-read-up before injection; over-clearance drops leak nothing; audited. |
| **LLM08 — Vector/embedding weakness** | REQ-023, REQ-041, REQ-022 | Per-agent isolated index; embeddings via arcllm (validated source); classified plaintext never in a shared store; index disposable/rebuildable. |
| **LLM09 — Misinformation** | REQ-033, REQ-053, REQ-070 | Confidence-gated action (`guessed` verifies first); grounded reflection only; degraded-recall surfaced not hidden. |
| **LLM10 — Unbounded consumption** | REQ-043, REQ-054, REQ-082 | Zero-LLM fast path; single-pass bounded retrieval; optional rerank over a *small* set only; rides SPEC-038 budgets. |
| **ASI01 — Goal hijack** | REQ-071 | Reflection writes policy/context only via system Curator; `identity.md` inode-locked; no free-write tool; goals never a reflection output. |
| **ASI03 — Identity/privilege abuse** | REQ-023 | Scope-ID isolation per agent DID; explicit audited team-share only. |
| **AU/AC (NIST)** | REQ-034, REQ-061, REQ-071 | Every mutation + recall drop audited to the tamper-evident chain (AU-2/9/10); no-read-up authorization on recall (AC-3/4). |

## Acceptance criteria (pillar-tied)

- **AC-1 (Modularity — clean package, REQ-001/002/003):** `arcmemory` installs standalone; architecture tests confirm no `arcmemory → arcagent` import; both old backends and their dead artifacts are asserted **absent**; `arcagent/core` NCLOC unchanged-or-lower.
- **AC-2 (Simplicity — zero-LLM fast path, REQ-010/011/012):** A capture adds an episodic event + daily bullet + entity bumps with **no LLM/embedding call** (asserted via a spy); cost is flat across a 10k-item store; sanitizer/privacy-filter/dedup run before write.
- **AC-3 (Scalability — glass-box + rebuildable, REQ-021/022):** Curated brain is human-editable markdown; deleting the entire derived index and running rebuild reproduces byte-identical FTS5/vector/cue-graph from files + raw stream.
- **AC-4 (Simplicity — sleep distills, REQ-030/031/032/034):** A scheduled `consolidate()` over a fixture day produces updated facts, ≥1 minted insight, ≥1 promoted procedure, decays a stale edge, keeps a salient one, writes a `was:` trail on a contradiction, and emits an audit event per mutation.
- **AC-5 (Simplicity — fused + multi-hop, REQ-040/042/043):** A semantically-related, lexically-disjoint query retrieves the right memory (proving vectors, not substrings); the result includes N-hop neighbors + surrounding raw-stream context; output respects top-k + token budget.
- **AC-6 (Differentiator — analogical retrieval, REQ-050/051/052/053):** A **planted structural probe** — a new situation matching a past `insight` with **zero lexical/semantic overlap** — retrieves that insight via BOTH trigger-embedding and cue-graph spreading, enriched with its instances; a `guessed` insight is flagged "verify first"; a never-recurring guessed insight decays out over simulated time.
- **AC-7 (Scalability — degrade + bounded, REQ-041/054/082):** With the embedder disabled, retrieval returns BM25+graph results and audits degraded mode (no exception); the optional rerank only runs over the small structural candidate set and is off by default at personal.
- **AC-8 (Security — no-read-up + injection-safe, REQ-060/061/062):** A `SECRET` memory is not injected into an `UNCLASSIFIED` context (dropped, audited, leaks nothing); a missing label fails closed at federal; every injected memory is boundary-marked; injection strings in a memory do not alter behavior.
- **AC-9 (Security — goal-lock + grounded reflection, REQ-070/071/072):** Reflection curates scored `policy.md` bullets via the existing ACE Curator (no second algorithm), never writes/proposes `identity.md`, stages `policy.pending` at federal; a session-less run still learns (bounded); SPEC-035 denial fires on a tool write to `policy.md`.
- **AC-10 (Modularity — swappable brain, REQ-080/081):** A fake `Brain` satisfying the Protocol type-checks under mypy strict and is config-selected by `arcagent` without touching arcagent core; the same loop runs at every tier with only stringency differences.
