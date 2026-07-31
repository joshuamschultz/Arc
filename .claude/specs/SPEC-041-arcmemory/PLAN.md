# PLAN — SPEC-041 arcmemory: dual-speed analogical memory

TDD throughout: write the failing test, watch it fail for the right reason, implement minimally, verify green, refactor. Tasks map **REQ → component (SDD §) → task**. Tags: `[component: pkg]` `[threat: ID]`. Legend **[REQ-…]**.

**Gating:** Phase 0 confirms the open questions (OQ-1..7) before building — do not start Phase 3+ until OQ-6 (embeddings scope) and OQ-3 (consolidation call-vs-loop) are answered. Absorb-then-delete: old backends are removed in Phase 8, after their parts are re-homed.

## Phase 0 — Decisions & scaffolding

- [ ] **T-000** Confirm OQ-1 (situation abstraction = reuse turn summary), OQ-2 (cue-vocab governance), OQ-3 (consolidation = bounded arcllm call, no loop), OQ-4 (insight confidence constants), OQ-5 (rerank tier default), OQ-6 (embeddings in arcllm scope), OQ-7 (reflection approval gate) with the product owner. Record in README. **No code.**
- [ ] **T-001** Scaffold the `arcmemory` package: `pyproject.toml` (deps `arctrust`/`arcllm`/`arcstore`; extras `[vec]`=sqlite-vec, `[local]`=sentence-transformers), src layout, `py.typed`, empty test tree. Add to the uv workspace + CI matrix. `[component: arcmemory]` **[REQ-001]**
- [ ] **T-002** Architecture regression test (TX.1): `arcmemory` imports none of `arcagent`/`arcrun`; DAG places it below arcagent. **Test:** import-graph assertion fails if `arcmemory→arcagent` appears. `[component: tests/architecture]` **[REQ-001]**
- [ ] **T-003** `types.py` — Pydantic models `Event, Fact, Entity, Procedure, Insight, Cue, Recall, Bundle, Scope, Situation, ConsolidationResult`. **Test:** round-trip serialize; mypy-strict clean. `[component: arcmemory.types]` **[REQ-020]**

## Phase 1 — arcllm embeddings capability (OQ-6)

- [ ] **T-010** `EmbeddingResponse` + `async embed(texts, *, model)` public API. **Test:** fake adapter returns fixed vectors; dims match; mypy-strict. `[component: arcllm]` **[REQ-041]**
- [ ] **T-011** Route `embed` through existing budget/rate/retry/audit/telemetry (SPEC-038). **Test:** an embed debits budget + emits telemetry; over-budget raises the standard breach. `[component: arcllm]` `[threat: LLM10]` **[REQ-082]**
- [ ] **T-012** Backends: local **`bge-small-en-v1.5`** default (D-8, ONNX/no-PyTorch, offline, deterministic), `all-MiniLM-L6-v2` speed fallback, optional provider endpoint, `none` sentinel. **Test:** local returns deterministic vectors with no network; `none` signals unavailable. `[component: arcllm]` `[threat: LLM08]` **[REQ-041]**
- [ ] **T-013** `async rerank(query, docs) -> scores` — cross-encoder `bge-reranker` (D-9, ONNX), rides the same budget/rate/audit. **Test:** reranks a small pair set on CPU; deterministic; `none` sentinel degrades to identity order. `[component: arcllm]` `[threat: LLM09]` **[REQ-054]**

## Phase 2 — Stores + storage substrate (`arcmemory/stores`, `index/graph`, `arcstore` schema)

- [ ] **T-020** **arcmemory-owned per-agent SQLite** (`workspace/memory/index.db`) schema: `episodic`, `chunks`, `fts_chunks` (FTS5), `edges`, `vec0` (sqlite-vec) — NOT arcstore (DC-1: arcstore is a closed 5-kind spool, no FTS5/vec). Optional: emit an `agent_event` telemetry row to arcstore per capture via `arcstore.record()`. **Test:** index.db created per agent workspace; extension load guarded; another agent's workspace is a separate file (hard isolation); optional arcstore telemetry row written. `[component: arcmemory.stores]` `[threat: LLM08]` **[REQ-021, REQ-023]**
- [ ] **T-021** `stores/episodic.py` — append raw event to arcstore + daily-log bullet to `memory/daily-log/YYYY-MM-DD.md`. **Test:** event persisted + bullet appended; ordering preserved. `[component: arcmemory.stores.episodic]` **[REQ-020, REQ-021]**
- [ ] **T-022** `stores/semantic.py` — entity markdown (frontmatter + fact triplets `pred: val .conf date | was:`) + graph edges; absorb `bio_memory/facts.py` + `entity_helpers.py`. **Test:** fact write + `was:` trail on contradiction; wiki-link edge created. `[component: arcmemory.stores.semantic]` **[REQ-020, REQ-032]**
- [ ] **T-023** `stores/procedural.py` + `stores/insight.py` — how-to cards (use-count) and insight cards (`statement, trigger, cues[], instances[], confidence, salience`). **Test:** card round-trips to/from markdown; insight carries trigger+cues+instances. `[component: arcmemory.stores]` **[REQ-020, REQ-050]**
- [ ] **T-024** `index/graph.py` — weighted edges with `hebbian_bump`, `decay` (`w·e^−λΔt`, salience `λ_eff=λ(1−βs)`), and `spreading_activation` (ACT-R base-level, lateral inhibition). **Test:** bump saturates at W; unreinforced edge decays below floor; salient edge survives; activation flows to N-hop neighbors. `[component: arcmemory.index.graph]` **[REQ-011, REQ-031, REQ-051]**
- [ ] **T-025** `index/rebuild.py` — re-derive `fts_chunks`+`vec0`+`edges` from files + raw stream. **Test:** wipe all derived tables → rebuild → byte-identical index + same retrievable set (index is disposable). `[component: arcmemory.index.rebuild]` `[threat: ASI06]` **[REQ-022]**

## Phase 3 — Fast path: deterministic capture (`arcmemory/capture.py`, `security.py`)

- [ ] **T-030** `security.py` — `sanitize` (char allowlist, size cap, injection-pattern drop), `privacy_filter` (strip secrets), `dedup` (windowed content hash). Absorb the existing `sanitize_text`. **Test:** injection string dropped; secret redacted; duplicate within window suppressed. `[component: arcmemory.security]` `[threat: ASI06, LLM01]` **[REQ-012]**
- [ ] **T-031** `capture.py` — sanitize→filter→dedup → append episodic + bullet → deterministic entity tag (vocab+regex) → `hebbian_bump` co-active pairs. **Zero LLM/embedding.** **Test:** a **spy** asserts no arcllm call during capture; event+bullet+edges written. `[component: arcmemory.capture]` **[REQ-010, REQ-011]**
- [ ] **T-032** Capture cost is flat. **Test (performance):** capture latency is constant (±) across a 10k-item store. Emit `memory.captured` audit. `[component: arcmemory.capture]` `[threat: AU-2]` **[REQ-010, REQ-034]**

## Phase 4 — Surface retrieval (`arcmemory/index/surface.py`)

- [ ] **T-040** `surface.index_if_needed()` — incremental, mtime-gated: embed new/changed chunks once via injected `embed`, store in `vec0`. **Test:** first index embeds all; unchanged → no re-embed; changed mtime → re-embed that file only. `[component: arcmemory.index.surface]` `[threat: LLM10]` **[REQ-040]**
- [ ] **T-041** `surface.search(text, top_k)` — vec cosine + BM25 + graph, **RRF fuse**, recency/decay-weight. **Test:** a no-lexical-overlap-but-semantically-related query retrieves the right chunk (embeddings, not substrings); fusion beats BM25-alone; newer/reinforced ranks higher. `[component: arcmemory.index.surface]` **[REQ-040]**
- [ ] **T-042** Degrade: on sqlite-vec load failure / embedder unavailable → BM25+graph only + `recall.degraded` signal, no exception. **Test:** embedder disabled → results returned, degraded emitted. `[component: arcmemory.index.surface]` **[REQ-041]**

## Phase 5 — Slow path: consolidation ("sleep") (`arcmemory/consolidate.py`, `distill.py`)

- [ ] **T-050** `distill.extract_facts(events)` — bounded arcllm structured completion → additive semantic facts + `was:` trails; confidence `1−e^−γ·hits`. **Test:** a fixture day yields updated facts; contradiction writes `was:` not overwrite; confidence rises with repeat mentions. `[component: arcmemory.distill]` `[threat: ASI06]` **[REQ-031, REQ-032, REQ-033]**
- [ ] **T-051** `distill.mint_insights(events, facts)` — arcllm → `Insight{statement, trigger, cues[], instances[]}`, `guessed` on first mint; cues added to the controlled vocabulary as graph nodes. **Test:** a cluster of structurally-similar/lexically-different episodes mints one insight with a surface-stripped trigger + ≥1 cue + instance links. `[component: arcmemory.distill]` **[REQ-050]**
- [ ] **T-052** `consolidate.run(window)` — orchestrate: extract facts, mint insights, `procedural.promote` (seq seen ≥ threshold), `graph.decay`, reindex touched chunks, rewrite affected files atomically (crash-safe manifest, absorb `DeepConsolidator`). **Test:** end-to-end over a fixture day: fact updated + insight minted + procedure promoted + stale edge decayed + salient kept; crash mid-write leaves a consistent manifest. `[component: arcmemory.consolidate]` **[REQ-030, REQ-031]**
- [ ] **T-053** Every consolidation mutation emits an `AuditEvent`. **Test:** fake sink captures one event per fact/insight/procedure/decay/file-write; chain verifies. `[component: arcmemory.consolidate]` `[threat: AU-2/9/10]` **[REQ-034]**
- [ ] **T-054** Cue de-dup/merge (embedding-cluster) bounds vocabulary drift (OQ-2). **Test:** two near-duplicate cues merge; instance links repoint. `[component: arcmemory.consolidate]` **[REQ-055]**

## Phase 6 — Structural / analogical retrieval — the centerpiece (`arcmemory/index/structural.py`)

- [ ] **T-060** `structural.trigger_index()` — embed insight `trigger`s into a separate `insight_trigger` vec table (kept apart from surface chunks). **Test:** triggers embedded + queryable; surface chunks not commingled. `[component: arcmemory.index.structural]` **[REQ-050, REQ-051]**
- [ ] **T-061** `structural.match(situation)` channel (a): abstract the current situation (default: reuse turn summary — OQ-1), embed, cosine-match trigger vectors. **Test:** a situation described at the mechanism level matches an insight whose trigger shares **no surface tokens** with the original episodes. `[component: arcmemory.index.structural]` **[REQ-051]**
- [ ] **T-062** `structural.match` channel (b): activate cue nodes from the situation, `graph.spreading_activation` → insight nodes. **Test:** lighting the situation's cues retrieves the insight with **zero lexical/semantic overlap** to its instances. `[component: arcmemory.index.structural]` **[REQ-051]**
- [ ] **T-063** Confidence gate: `known` → actionable anchor; `guessed` → surfaced tentatively (flag "verify first"). **Test:** a once-minted insight is flagged; a corroborated one is not. `[component: arcmemory.retrieve]` `[threat: LLM09]` **[REQ-053]**
- [ ] **T-064** Enrichment: traverse matched insight → instances (episodes) + related entities + adjacent insights + surrounding raw-stream context (bounded hops). **Test:** the bundle anchored on the insight includes its instances + N-hop neighbors + adjacent stream events. `[component: arcmemory.retrieve]` **[REQ-042, REQ-052]**
- [ ] **T-065** Cross-encoder rerank (via `arcllm.rerank`, D-9) over the small candidate set (top-20→top-5), tier-gated: personal OFF + **score-margin fallback** (rerank only when top-1/top-2 margin < threshold), enterprise/federal ON (scores audited at federal). Reranker NOT in the agent tool surface. **Test:** personal skips rerank unless margin close; enterprise reranks bounded set; federal audits scores; verdict never enters agent context. `[component: arcmemory.retrieve]` `[threat: LLM09, LLM10]` **[REQ-054]**

## Phase 7 — Retrieve orchestration + security gate (`arcmemory/retrieve.py`, `security.py`)

- [ ] **T-070** `retrieve()` — fuse surface+structural (RRF), confidence-gate, then `gate_no_read_up` (reuse `arctrust.dominates`/`parse_classification`; map `SessionACL.classification` onto the ladder; drop over-clearance). **Test:** SECRET dropped for UNCLASSIFIED; CUI kept for SECRET; **no new comparator** (import assertion). `[component: arcmemory.retrieve]` `[threat: LLM02, AC-3]` **[REQ-040, REQ-060]**
- [ ] **T-071** Fail-closed missing label (federal `strict` rejects; personal warns+defaults); dropped memory audited, leaks nothing via rank/count/error. **Test:** unlabeled rejected at federal, defaulted at personal; drop audited; kept list + errors contain no trace. `[component: arcmemory.retrieve]` `[threat: LLM02, AU-2]` **[REQ-060, REQ-061]**
- [ ] **T-072** Boundary-mark (`<memory-result>` source+score+confidence, data-not-instructions) + top-k + token budget (truncate lowest first). **Test:** each item boundary-wrapped; over-budget truncates from bottom; injection-laden memory rendered inert. `[component: arcmemory.security]` `[threat: LLM01]` **[REQ-062, REQ-043]**

## Phase 8 — arcagent wiring + delete old backends (`arcagent/modules`, one core seam)

- [ ] **T-080** (core) Add `query` to `assemble_system_prompt` + `agent:assemble_prompt` payload; callers pass `task`/`input_text`. **Signature/payload only.** **Test:** emitted event carries turn text; existing assembly tests pass; **core NCLOC unchanged-or-lower**. `[component: arcagent.core]` **[REQ-002]**
- [ ] **T-081** Thin `modules/memory` wiring (real seams — DC-4): `Brain.capture()` on `agent:post_tool` + `agent:pre_respond` (NOT `post_respond` — no such event); `Brain.retrieve()` on `agent:assemble_prompt` @ priority 50 → `sections["recall"]` (not `memory_context`), once-per-turn sentinel; use `EventContext` (not `HookContext`); de-duplicated `memory` tool. **Test:** capture fires on tool events; recall injected once across spawn double-assembly; single `memory_search` tool registered. `[component: arcagent.modules.memory]` **[REQ-002]**
- [ ] **T-082** Schedule `Brain.consolidate()` via `@background_task(interval=N)` polling an **event-count/idle trigger** (DC-5: `@background_task` is interval-seconds, not cron; consolidate when accumulated events cross a threshold or after idle). **Test:** the poll invokes `consolidate` when the trigger fires, not before (fake clock + event counter). `[component: arcagent.modules.memory]` **[REQ-030]**
- [ ] **T-083** Keep `memory_acl` wired at priority-10 over the `Brain` calls. **Test:** ACL veto still fires before recall/capture. `[component: arcagent.modules.memory_acl]` `[threat: AC-3]` **[REQ-060]**
- [ ] **T-084** **Delete** `modules/bio_memory` + `modules/memory` and dead artifacts (facade classes, duplicate tool/hook, dead `embedding_model`/`search_weight_vector`, `ctx.data["memory_context"]`) — same change, after re-homing. **Test:** assert both modules + `memory_context` key + dead config are **absent**; no test still asserts the retired path. `[component: arcagent.modules]` **[REQ-003]**

## Phase 9 — Grounded reflection → existing ACE (`modules/policy/reflection.py`) [retained]

- [ ] **T-090** `ReflectionGrounding` + `reflect_and_curate()` feeds the **existing** `PolicyEngine._reflect`→`_curate` (no second algorithm); empty grounding writes nothing. **Test:** grounded input yields scored bullets via the real engine; empty → zero mutations. `[component: arcagent.modules.policy]` `[threat: LLM09]` **[REQ-070]**
- [ ] **T-091** Session-less automated run grounds on the consolidation episode (close the gap); `write_approval` tier-defaulted (federal stages `policy.pending`). **Test:** session-less run produces a bounded write; federal stages, personal auto-applies. `[component: arcagent.modules.policy]` **[REQ-072]**
- [ ] **T-092** Goal-lock + bounds: writes only `policy.md`/`context.md` (never `identity.md`, no free-write tool); reuse score-clamp/prune/cap/sanitize; audit every mutation; SPEC-035 tool-write denial still fires. **Test:** `identity.md` never targeted; crafted bullet clamped/pruned/sanitized; `policy.reflected` audit emitted. `[component: arcagent.modules.policy]` `[threat: ASI01, ASI06]` **[REQ-071]**

## Phase 10 — Integration & security

- [ ] **T-100** **AC-2** — zero-LLM capture end-to-end through the real hook (spy asserts no arcllm on the hot path). **[REQ-010]**
- [ ] **T-101** **AC-4** — scheduled `consolidate()` runs via the proactive engine, mints an insight + promotes a procedure + decays a stale edge + writes a `was:` trail, all audited. **[REQ-030, REQ-034]**
- [ ] **T-102** **AC-5** — semantic (no-lexical-overlap) recall works end-to-end via the real assembler; result carries N-hop neighbors + surrounding context; respects top-k+budget. **[REQ-040, REQ-042]**
- [ ] **T-103** **AC-6 — the differentiator.** A **planted structural probe** (a new situation instancing a past insight with **zero lexical/semantic overlap**) is retrieved via BOTH trigger-embedding and cue-graph spreading, enriched with its instances; a never-recurring `guessed` insight decays out over simulated time. **[REQ-050, REQ-051, REQ-052, REQ-053]**
- [ ] **T-104** **AC-8** — SECRET-into-UNCLASSIFIED blocked+audited via the real recall path; missing label fail-closed at federal; injection-laden memory inert. **[REQ-060, REQ-062]**
- [ ] **T-105** **AC-3 / AC-7** — rebuild reproduces indices byte-identically; embedder-disabled degrades to BM25+graph and still injects. **[REQ-022, REQ-041]**
- [ ] **T-106** **AC-9** — reflection feeds the real `PolicyEngine`, never `identity.md`; session-less run learns bounded; federal stages `policy.pending`. **[REQ-070, REQ-071, REQ-072]**

## Phase 11 — Gates

- [ ] **T-110** `ruff check`/`format` clean; `mypy --strict` clean across arcmemory + touched packages (arcllm, arcagent, arcstore). Fix any pre-existing debt seen in-flight (leave-it-correct).
- [ ] **T-111** Coverage ≥ 80% line / ≥ 75% branch on arcmemory; **core NCLOC ≤ prior** (logic left core for arcmemory); `pip-audit` clean for `sqlite-vec` + embedder deps.
- [ ] **T-112** Run the **full** package matrix together (roadmap lesson: cross-package tests broke on prior specs); architecture tests (TX.1) green incl. the new `arcmemory` DAG assertion; no test asserts a retired backend.
