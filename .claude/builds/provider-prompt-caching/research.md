# provider-prompt-caching — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-387–D-407 (20 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)

**Phase**: build/deepen | **Status**: IMPLEMENTED (SPEC-029, all gates green) | **Total decisions**: 11
**Priority framework**: simplicity > modularity > security > scalability (principled-coder)
**Source**: caching audit + 5-agent deepen research. Enriched doc: `.claude/specs/llm-prompt-caching/DEEPENED.md`

#### Summary

arcrun's loop is cache-correct by construction (append-only, stable system prompt, deterministic
tool + parallel-result ordering), but arcllm emits no cache directive so Anthropic caching is OFF
and OpenAI/Gemini/Ollama caching is implicit + unmeasured. Fixes split cleanly by package:
arcllm sets breakpoints + reads telemetry; arcrun guarantees an immutable ordered tool set;
arcagent owns compaction. A live per-turn cache-buster was found in arcagent's `ContextManager`
(sliding-window prune every turn >70%).

#### Decisions

| # | Decision | Choice | Priority | Rationale / Tier Notes |
|---|----------|--------|----------|------------------------|

#### Open question (blocking D-396 method choice)

What IS compaction — truncation, an LLM summary turn, structured detail-extraction to bullet
fields, or a hybrid? Under research (Anthropic guidance / production teams / academic methods).
Decision to be recorded once research synthesizes.

#### `/specify` scoping

- **SPEC A — arcllm caching**: D-387..D-390, D-393, D-395-doc. Self-contained, highest ROI.
- **SPEC B — arcrun tool immutability**: D-391, D-392, D-394 (arcrun half).
- **SPEC C — arcagent compaction**: D-396, D-397 (+ arcrun D-394 contract). Blocked on method research.

Dependency: SPEC A Anthropic *tool-block* cache hits need SPEC B stable ordering (system/message hits land regardless).

#### D-396 Resolution — Compaction Method (2026-07-01)

**Research**: 3 web agents (Anthropic guidance / production teams / academic + benchmarks). Convergent.
**Key evidence**: JetBrains "Complexity Trap" (arXiv:2508.21433) — masking ≈/> LLM summarization at
~half cost; summarization causes 15% trajectory elongation by smoothing over "stuck" signals.
Factory.ai (36,611 prod msgs) — structured schema-anchored extraction 3.70 > generic Anthropic 3.44 /
OpenAI 3.35; "structure forces preservation." "Less Context, Better Agents" (arXiv:2606.10209) —
pruning+structured running summary **91.6%** vs pruning-only 79% vs full-context 71%, at flat token
cost; recency window plateaus ~N=5 tool calls, summary window >3 adds nothing. Manus — append-only +
file offload ("restorable compression"); KV-cache hit rate is THE production metric (100:1 in:out).
Recursive summarization = highest drift risk (arXiv:2602.09789 knowledge-overwriting/semantic-drift) —
avoid as default.

| # | Decision | Choice | Priority | Rationale |
|---|----------|--------|----------|-----------|

**Trigger/hysteresis (confirms D-396 shape):** trigger off provider-reported tokens where available
(`token_ratio()`), estimate as pre-call guard. Compact **deep** (~50%) so many append-only turns follow
before the next boundary — avoids threshold hover/thrash. Single-shot summary per boundary (no recursion);
if incremental, merge only the newly-evicted delta into the persisted summary (MemGPT/Factory pattern),
never regenerate-from-scratch every round.

**Layering (matches Anthropic's stack):** append-only (default) → mask+offload at boundary (cheap,
restorable) → structured summary at boundary (lossy but schema-guarded) → emergency truncate (floor).
Retrieval/memory offload is orthogonal (cross-session), not an in-loop compaction lever.

D-396 status: **RESOLVED** → see D-398..D-402. Method no longer blocking SPEC C.

#### SPEC-029 /review follow-ups (2026-07-01) — applied

Swarm review (security/clean-code/architecture/QA) found 2 BLOCKING correctness bugs — both
pre-existing latent, both exposed/worsened by SPEC-029 making transform_context identity. Fixed:

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|

Also: fail-open flush now logs `exc_info=True`; compaction audit emitted inside the lock; dead
`usage_ratio` removed; stale docstrings (context.py, manager.py) corrected; `context_manager`
typed `ContextManager | None` (removed `Any`-laundering). Regression tests added:
reassembly-after-compaction, estimate-trigger, emergency-keeps-newest. All gates green.

---

---
