# SPEC-029 — Prompt Caching & Context Control

**Status**: COMPLETE | **Created**: 2026-07-01 | **Type**: cross-package (arcllm + arcrun + arcagent)
**Priority framework**: Simplicity → Modularity → Security → Scalability (principled-coder)

## One-line

Turn provider prompt caching ON and make context compaction correct, structured, and
cache-preserving — across all three packages, with each fix landing on the correct side of the
package boundary.

## Why

Audit + 5-agent deepen + 3-agent methods research found:
- **arcrun's loop is cache-correct by construction** (append-only, stable prefix) — but has a
  latent mutable-registry risk.
- **arcllm emits no cache directive** → Anthropic caching is fully OFF; OpenAI/Gemini caching
  works implicitly but is **unmeasured**; Ollama residency is unmanaged.
- **arcagent has a split-brain compaction**: a well-built discrete `session.compact()` (30/70
  split, pre-flush to `context.md`, write-back) AND a per-turn `transform_context` that
  prunes/truncates a **sliding window every turn > 70%** — busting the cache on every turn of
  exactly the long-running agents where caching matters most.
- Both existing summarizers use **freeform prose prompts** ("summarize concisely") — the
  empirically weakest method (Factory.ai 36,611-msg eval: structured 3.70 > prose 3.44/3.35;
  JetBrains: prose summarization causes 15% trajectory elongation).

## Scope (one spec, three package-scoped work streams)

| Stream | Package | Essence |
|--------|---------|---------|
| A | arcllm | Anthropic auto-breakpoints (behind flag, no type change) + OpenAI/Gemini telemetry read-back + Ollama residency doc |
| B | arcrun | Freeze per-run tool set (immutable + ordered); append-only `transform_context` contract; no caching concept in the loop |
| C | arcagent | `transform_context` → identity; `session.compact()` becomes sole compaction path; structured summary schema; masking + flush at the boundary; deep/debounced trigger |

## Decisions (source)

D-387…D-397 (deepened build decisions) + D-398…D-402 (compaction-method resolution) in
`.claude/decisions-log.md` → "Provider Prompt Caching" section (2026-07-01).
Enriched research: `.claude/specs/llm-prompt-caching/DEEPENED.md`.

## Key sources

- Anthropic prompt caching + context editing + compaction + memory-tool docs (platform.claude.com)
- Anthropic "Effective context engineering" + "Effective harnesses for long-running agents"
- Manus context-engineering; JetBrains "Complexity Trap" (arXiv:2508.21433); Factory.ai
  compression eval; "Less Context, Better Agents" (arXiv:2606.10209); MemGPT/Letta.

## Boundary invariants (must hold at "done")

- `cache_control` lives ONLY in `arcllm/adapters/anthropic.py`. Not in arcllm shared types, not in arcrun, not in arcagent. [D-393]
- arcrun knows about **ordering/immutability**, never caching. [D-391, D-393]
- All LLM calls (incl. summarization) go through arcllm — arcagent never calls a provider directly. [CLAUDE.md]
- `transform_context` is append-only; compaction is a discrete, persisted, boundary event. [D-394, D-396]

## Definition of Done

- [x] Anthropic cache breakpoints emitted (≤3, tools→system→messages); usage read-back verified. `test_prompt_caching.py`
- [x] OpenAI + Gemini `cache_read_tokens` populated (stream + non-stream); `0`≠`None` preserved.
- [x] `ToolRegistry` immutable per run; mid-run `add`/`remove` raises + emits `tool.mutation_denied`; `list_schemas` memoized.
- [x] `transform_context` is append-only (identity + emergency-only valve); prefix-stability proven across turns.
- [x] Compaction uses the structured schema; `goal`/`constraints` preserved verbatim.
- [x] Masking + structured flush happen at the boundary, persisted; not per turn.
- [~] Compaction boundary behaviors verified by unit tests (deep-split, mask+persist, audit); single full-E2E deferred (T-040).
- [x] `ruff check` + `mypy --strict` clean across all three packages; full suites green (941 + 435 + 3295 = 4671 tests).
- [x] Audit event `context.compaction` emitted (before/after counts).
- [x] CLAUDE.md structure pointer corrected (root + arcagent).

## Learnings

- **Split-brain, not greenfield.** arcagent already had a discrete `session.compact()` (30/70 split, `context.md` flush, write-back). The bug was the *second* mechanism — per-turn `transform_context` pruning a sliding window — busting cache every turn. Fix was to collapse to one path, not build compaction.
- **Both summarizers were freeform prose** ("summarize concisely") — the empirically weakest method. Swapped to a schema (goal/constraints/progress/key_facts/files_modified/decisions/rejected_approaches/open_questions/next_step); `goal`+`constraints` verbatim.
- **`transform_context` can't be pure identity**: the ReAct loop runs many model calls within one dispatch, but `maybe_compact` only fires between dispatches — so an emergency-only truncation valve stays as the in-run floor (D-402).
- **mypy scope surprise**: expanding to "mypy --strict = 0 across all packages" (user call) cleared 48 arcllm errors (root cause: `LLMProvider.name` declared as writeable attr vs read-only property; added `model_name` too), 7 arcagent module errors, plus a duplicate-module exclude for skill scaffolding scripts. arcrun was already clean. A stale `.mypy_cache` masked which override errors were real — always clear it before counting.
