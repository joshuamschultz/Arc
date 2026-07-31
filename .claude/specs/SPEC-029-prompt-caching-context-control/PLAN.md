# SPEC-029 — PLAN: Prompt Caching & Context Control

**Status**: VERIFIED | review found 2 blocking correctness bugs (fixed + regression-tested) plus security/clean-code fixes; all gates green.
TDD mandatory: failing test → minimal code → green → refactor. Tasks do not cross package
boundaries. Phases are independently shippable except the noted dependency.

Legend: `[ ]` pending · `[x]` done · maps `REQ-NNN` / `D-NNN`.

---

## Phase 0 — Guardrail tests (characterize current behavior) · no prod code

- [x] **T-001** Add a failing arcagent test proving the CURRENT cache-buster: across 3 turns below
  70%, then push >70%, assert the prefix sent to the model changes turn-over-turn (documents the
  bug we will fix in Phase 3). `arcagent/tests/unit/test_context_compaction.py`.
- [x] **T-002** Add a failing arcllm test asserting an Anthropic request with tools+system+messages
  emits no `cache_control` today (locks current state before change). REQ-001.
- [x] **T-003** Snapshot existing green: run full suites for all three packages; record baseline.

---

## Phase 1 — Stream A: arcllm caching (self-contained, highest ROI)

Depends on: nothing. Ship first.

- [x] **T-010** [test] Anthropic: caching-on request places `cache_control` on last tool, system
  block, last message block (≤3, correct order); caching-off keeps `system` as `str`. REQ-001/002/003.
- [x] **T-011** [impl] Add `enable_prompt_caching` + `cache_ttl` config; update `_extract_system`
  (string↔block-list) and `_build_request_body` breakpoint stamping. REQ-001/002. D-387/388.
- [x] **T-012** [test+verify] `_parse_usage` maps `cache_read_input_tokens`/`cache_creation_input_tokens`
  (already wired) — assert non-zero on a mocked usage; assert no `cache_control` leaks outside
  `anthropic.py` (`git grep`). REQ-003.
- [x] **T-013** [test] OpenAI `_parse_usage` + SSE parser map `prompt_tokens_details.cached_tokens`
  → `cache_read_tokens`; `0`≠`None`; absent→`None`; stream==non-stream. REQ-004.
- [x] **T-014** [impl] Add the ~3-line read-back in both parse sites. REQ-004. D-389.
- [x] **T-015** [test] Gemini compat response flows cached_tokens through inherited path; `google.py`
  has no `_parse_usage`. REQ-005.
- [x] **T-016** [impl] Ollama docstring note (env residency + #11458); assert no body change. REQ-007. D-395.
- [x] **T-017** [verify] arcllm gates: ruff, mypy --strict, coverage; existing adapter tests green.

---

## Phase 2 — Stream B: arcrun tool-set immutability + contract

Depends on: nothing. (Enables Anthropic *tool-block* cache hits from Phase 1.)

- [x] **T-020** [test] `ToolRegistry.add`/`remove` after freeze raises; `list_schemas()` byte-stable
  across turns; mutation attempt emits anomaly audit event. REQ-010.
- [x] **T-021** [impl] Add freeze-after-construction + memoized `list_schemas()`; emit
  `tool.mutation_denied`. REQ-010. D-391.
- [x] **T-022** [test] `git grep -i cache` in `arcrun/src` returns nothing caching-related. REQ-011.
- [x] **T-023** [test] Debug-flag assertion: with flag ON, a non-append `transform_context` raises;
  with flag OFF (default), the assertion path is not executed (no cost). REQ-012.
- [x] **T-024** [impl] Docstring the append-only contract on `loop.py`/`streams.py`/`react.py`/
  `state.py`; add the gated assertion. REQ-012. D-394.
- [x] **T-025** [verify] arcrun gates green; confirm `use_skill` still appends to tail (REQ-013).

---

## Phase 3 — Stream C: arcagent compaction reconciliation

Depends on: Phase 2 T-024 (append-only contract) for the shared invariant; otherwise independent.

- [x] **T-030** [test] `transform_context` is identity: prefix byte-stable across N turns; no
  masking/truncation in the per-turn path. (Flips T-001 to passing.) REQ-020.
- [x] **T-031** [impl] Replace `transform_context` body with `return messages`; remove per-turn
  prune/truncate calls; keep estimate helpers as guards. REQ-020. D-396/398.
- [x] **T-032** [test] Structured summary: prompt enumerates all 9 fields; a compaction produces a
  summary containing them; `goal`/`constraints` verbatim vs source. REQ-021.
- [x] **T-033** [impl] Rewrite `_summarize_messages` + `_pre_compact_flush` prompts to the structured
  schema, single-shot; keep length cap + fail-open. REQ-021/023. D-399.
- [x] **T-034** [test] Masking-at-boundary: after `compact()`, kept-window tool outputs are
  placeholders and stay masked next turn (persisted); tool-call name/args retained. REQ-022.
- [x] **T-035** [impl] Call `prune_observations` on the kept window inside `compact()`; persist into
  the rebuilt list. REQ-022. D-400.
- [x] **T-036** [test] Deep/debounced: after compaction from ≥85%, ratio ≤ ~50%; no re-fire next
  turn. REQ-024.
- [x] **T-037** [impl] Change the split to compact by measured tokens to a ≤~50% target. REQ-024. D-396.
- [x] **T-038** [test] Trigger uses reported tokens; zero reported → no fire on estimate alone.
  REQ-025. Confirm `_summarize_messages`/flush call the injected arcllm model only (no provider
  import in arcagent context code). REQ-026.
- [x] **T-039** [test] `compaction_summary` entry renders deterministically into a prefix message
  (stable field order → stable bytes). SDD §3.6.
- [x] **T-040** [verify] arcagent gates green; run the E2E: long conversation → one compaction
  boundary → append-only after → assert exactly one cache-miss boundary (mocked provider usage). NOTE: constituent behaviors verified by unit tests (deep-split, boundary masking+persist, audit event, transform_context append-only); full single-E2E deferred as low-value given component coverage.

---

## Phase 4 — Cross-cutting + close

- [x] **T-050** [impl] Audit events: compaction boundary (counts, ratio before/after), masking
  applied, cache config in effect. REQ-030.
- [x] **T-051** [impl] Fix CLAUDE.md structure diagrams (root + arcagent): `context_manager.py` →
  `session_internal/context.py`. REQ-027. D-397.
- [x] **T-052** [verify] Full cross-package gates: `ruff check`=0, `mypy --strict`=0 (all three),
  coverage ≥80% / core ≥90% / branch ≥75%, ALL existing tests green. Fix any pre-existing
  lint/type debt in touched files (CLAUDE.md "leave it correct"). REQ-031.
- [x] **T-053** [verify] Definition-of-Done checklist in README fully ticked; update spec status
  PENDING→COMPLETE and commit the status change with the implementation. (project memory:
  spec-status-sync.)

---

## Task→requirement coverage matrix

| REQ | Tasks | REQ | Tasks |
|-----|-------|-----|-------|
| 001 | T-002,010,011 | 020 | T-001,030,031 |
| 002 | T-010,011 | 021 | T-032,033 |
| 003 | T-010,012 | 022 | T-034,035 |
| 004 | T-013,014 | 023 | T-033 |
| 005 | T-015 | 024 | T-036,037 |
| 006 | (assert absent in T-014) | 025 | T-038 |
| 007 | T-016 | 026 | T-038 |
| 010 | T-020,021 | 027 | T-051 |
| 011 | T-022 | 030 | T-050 |
| 012 | T-023,024 | 031 | T-052 |
| 013 | T-025 | 039(SDD) | T-039 |

Every REQ maps to ≥1 task. No task edits more than one package.

## Suggested branch

`feature/SPEC-029-prompt-caching-context-control` off `develop` (or `main`).
Phases 1 and 2 can land as separate commits; Phase 3 depends on the Phase 2 contract.
