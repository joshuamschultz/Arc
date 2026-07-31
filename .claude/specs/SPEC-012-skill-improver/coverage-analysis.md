# SPEC-012 Skill Improver Module -- Coverage Analysis

**Date**: 2026-02-26
**177 tests passing** | **All green**

---

## Coverage Summary

| Metric | Value | Target | Status |
|--------|-------|--------|--------|
| **Line Coverage** | **89%** | >= 80% | PASS |
| **Branch Coverage** | **85%** | >= 75% | PASS |
| **Total Statements** | 860 | -- | -- |
| **Missed Statements** | 95 | -- | -- |
| **Total Branches** | 220 | -- | -- |
| **Partial Branches** | 25 | -- | -- |

**Verdict: PASS** -- All quality gates met.

---

## Per-File Coverage

| File | Stmts | Miss | Branch | BrPart | Line% | Branch% | Category |
|------|-------|------|--------|--------|-------|---------|----------|
| `__init__.py` | 3 | 0 | 0 | 0 | 100% | 100% | Config |
| `config.py` | 18 | 0 | 0 | 0 | 100% | 100% | Config |
| `models.py` | 121 | 1 | 8 | 0 | 99% | 100% | Business Logic |
| `evaluator.py` | 53 | 0 | 8 | 0 | 100% | 100% | Business Logic |
| `reflector.py` | 51 | 0 | 16 | 0 | 100% | 100% | Business Logic |
| `guardrails.py` | 62 | 1 | 24 | 0 | 99% | 100% | Critical/Safety |
| `pareto.py` | 71 | 2 | 30 | 3 | 97% | 90% | Business Logic |
| `candidate_store.py` | 80 | 3 | 18 | 2 | 96% | 89% | Audit/Safety |
| `trace_collector.py` | 160 | 20 | 42 | 7 | 88% | 83% | Business Logic |
| `engine.py` | 103 | 18 | 26 | 5 | 83% | 81% | Business Logic |
| `skill_improver_module.py` | 138 | 50 | 48 | 8 | 64% | 83% | Facade/Orchestrator |

---

## Critical Gaps Analysis

### P0 -- CRITICAL (Security/Safety Code, Target >= 90%)

#### 1. `skill_improver_module.py` -- 64% line coverage (50 lines missed)

This is the **facade** orchestrating the entire module. The untested code includes:

**Missed lines and what they do:**

- **Lines 79-83** (`shutdown`): Awaiting background tasks before teardown. If background optimization tasks are in-flight and shutdown is called, this code gathers them. *Untested failure mode: what happens if a background task raises during shutdown?*

- **Lines 102-107** (`_on_post_respond`): The trigger that checks usage thresholds and spawns background optimization. This is the **primary activation path** for the entire optimization loop. Never exercised in tests.

- **Lines 123-124** (`_on_ready`): The fallback path where `skill_registry` is missing from the ready event. Warning logged, trace collection disabled.

- **Lines 136-188** (`_optimize_skill`): The **entire optimization orchestration method** -- loads traces, checks eligibility, gets skill path, gets eval model, creates evaluator/reflector/optimizer, runs optimization, applies result, rescans registry, emits telemetry. This is 52 lines of critical business logic that is completely untested at the unit level.

- **Lines 197-200** (`_get_skill_path`): Skill path lookup from registry. Both the found and not-found paths are untested.

- **Lines 204-212** (`_get_eval_model`): Lazy initialization of the eval model. Untested.

- **Lines 298, 302-304** (`_handle_skill_rollback`): The path where rollback succeeds but the skill file needs to be written via `atomic_write_text`. Only partially covered.

**Impact**: The primary orchestration path (`_on_post_respond` -> `_optimize_skill`) that connects trace collection to actual optimization is entirely untested. A regression here silently disables all skill improvement.

#### 2. `guardrails.py` line 116 -- `get_generation` method

- **Line 116**: The `get_generation` method that returns the current generation count for a skill. While simple, it's used by the engine to track generation limits. The `.get(skill_name, 0)` return path for unknown skills is not exercised.

**Impact**: Low standalone risk, but this feeds into generation-limit guardrail enforcement.

---

### P1 -- HIGH (Business Logic, Target >= 95%)

#### 3. `engine.py` -- 83% line coverage (18 lines missed)

**Missed lines:**

- **Line 62**: The `split_idx >= len(shuffled)` branch in `split_traces`. This handles the edge case where ratio=1.0 (or very close) would leave zero holdout traces. *Not tested with boundary ratio values.*

- **Lines 148-152**: The stagnation path triggered when `_reflector.reflect()` returns empty string (LLM failure during reflection). The test hits stagnation via no-improvement but not via empty reflection.

- **Lines 167-168**: The stagnation path triggered when `_guardrails.validate_candidate()` rejects a mutation. Guardrail rejection incrementing stagnation and breaking the loop is untested.

- **Lines 172-188**: The **successful mutation path** in the optimization loop -- evaluating the mutation on holdout, computing scores, calling `frontier.add_if_improves`, and continuing. While `test_returns_optimize_result` exercises part of this, the specific branch where `add_if_improves` returns `False` (leading to stagnation increment) vs `True` (resetting stagnation) is only partially covered.

**Impact**: The optimization loop's distinct exit paths (stagnation via empty reflection, stagnation via guardrail rejection, successful mutation but insufficient improvement) are not individually verified.

#### 4. `trace_collector.py` -- 88% line coverage (20 lines missed)

**Missed lines:**

- **Lines 99-100**: OSError when reading skill file during indexing. `_expected_tools[skill.name] = []` fallback.

- **Lines 115-116**: ValueError/OSError when resolving a file path in `on_post_tool`. Protects against malformed paths.

- **Line 147**: Guard clause in `_record_tool_call` when `_active_span is None`. Redundant guard already checked by caller, but defense-in-depth.

- **Lines 157, 159-160**: The `is_vetoed` and `isinstance(result, Exception)` branches in `_record_tool_call`. Vetoed tool calls and error tool calls are never exercised.

- **Lines 195-198**: The heuristic outcome paths: `all errors = failure` and `mixed = partial`. Only `success` (no errors) is tested.

- **Lines 229-230**: `json.JSONDecodeError` recovery when loading a corrupted index file.

- **Lines 240-241**: Index update for `failure` outcome (incrementing failure count).

- **Lines 266-267, 270, 274-275**: Error handling in `load_traces` -- OSError on file read, skipping blank lines, and malformed JSON line recovery.

**Impact**: Error/vetoed tool call recording and corrupted file recovery are defense-in-depth paths important for production resilience.

#### 5. `pareto.py` -- 97% line / 90% branch

**Missed lines:**

- **Line 75**: `_find` returning `None` when parent ID is not in the frontier (the `add_if_improves` path where parent exists but is not in frontier). The test hits `parent is None` via `add` delegation, but not the explicit `_find` returning None path.

- **Line 109**: `ParetoFrontier.from_dict` iterating over empty candidates list. Minor.

**Impact**: Low. Edge case in parent lookup.

#### 6. `candidate_store.py` -- 96% line / 89% branch

**Missed lines:**

- **Line 91**: `load` returning `None` path (candidate file doesn't exist). Tested indirectly via rollback, but the `load` method itself returning None for a missing candidate is not directly asserted.

- **Lines 133-134**: `load_manifest` fallback when JSON is corrupted (`json.JSONDecodeError` or `OSError`). Returns empty default dict.

**Impact**: Corrupted manifest recovery is important for production resilience. The store is the persistence layer for audit-critical data (NIST AU-3).

---

### P2 -- MEDIUM (Nice to Have)

#### 7. `models.py` line 217 -- `OptimizeResult.to_dict`

The `OptimizeResult.to_dict()` serialization method is unused in tests. It's used for telemetry/audit in the module facade. Low risk since the method is straightforward dictionary construction.

---

## Recommended Additional Tests (Prioritized)

### Phase 1: Critical (P0) -- Must Fix

These address the 64% coverage on `skill_improver_module.py`, which is the orchestrator.

| # | Test | File | Lines Covered | Effort |
|---|------|------|---------------|--------|
| 1 | `test_on_post_respond_triggers_optimization` | test_skill_improver_module.py | 102-107 | Medium |
| 2 | `test_optimize_skill_full_path` | test_skill_improver_module.py | 136-188 | High |
| 3 | `test_optimize_skill_ineligible_traces` | test_skill_improver_module.py | 140-142 | Low |
| 4 | `test_optimize_skill_no_eval_model` | test_skill_improver_module.py | 150-153 | Low |
| 5 | `test_optimize_skill_no_skill_path` | test_skill_improver_module.py | 146-147 | Low |
| 6 | `test_optimize_skill_seed_is_best` | test_skill_improver_module.py | 170-171 | Low |
| 7 | `test_shutdown_with_background_tasks` | test_skill_improver_module.py | 79-83 | Medium |
| 8 | `test_on_ready_no_skill_registry` | test_skill_improver_module.py | 123-124 | Low |
| 9 | `test_rollback_writes_skill_file` | test_tools.py | 298, 302-304 | Low |

**Expected coverage increase**: skill_improver_module.py from 64% to ~90%+ (covering ~45 of 50 missed lines).

### Phase 2: High Impact (P1) -- Should Fix

| # | Test | File | Lines Covered | Effort |
|---|------|------|---------------|--------|
| 10 | `test_vetoed_tool_call_recorded` | test_trace_collector.py | 157 | Low |
| 11 | `test_error_tool_call_recorded` | test_trace_collector.py | 159-160 | Low |
| 12 | `test_heuristic_failure_outcome` | test_trace_collector.py | 195-196 | Low |
| 13 | `test_heuristic_partial_outcome` | test_trace_collector.py | 197-198 | Low |
| 14 | `test_corrupted_index_recovery` | test_trace_collector.py | 229-230 | Low |
| 15 | `test_malformed_trace_line_skipped` | test_trace_collector.py | 274-275 | Low |
| 16 | `test_stagnation_via_empty_reflection` | test_engine.py | 148-152 | Medium |
| 17 | `test_stagnation_via_guardrail_rejection` | test_engine.py | 167-168 | Medium |
| 18 | `test_split_traces_boundary_ratio` | test_engine.py | 62 | Low |
| 19 | `test_corrupted_manifest_recovery` | test_candidate_store.py | 133-134 | Low |
| 20 | `test_skill_file_read_oserror` | test_trace_collector.py | 99-100 | Low |
| 21 | `test_failure_count_in_index` | test_trace_collector.py | 240-241 | Low |

**Expected coverage increase**: trace_collector.py from 88% to ~95%, engine.py from 83% to ~93%.

### Phase 3: Low Priority (P2)

| # | Test | File | Lines Covered | Effort |
|---|------|------|---------------|--------|
| 22 | `test_optimize_result_to_dict` | test_models.py | 217 | Low |
| 23 | `test_pareto_find_returns_none` | test_pareto.py | 75 | Low |
| 24 | `test_pareto_from_dict_empty` | test_pareto.py | 109 | Low |
| 25 | `test_get_generation_unknown_skill` | test_guardrails.py | 116 | Low |

---

## Integration Test Assessment

The current 4 integration tests verify:
- Trace collection via the module lifecycle (good)
- Guardrail enforcement for insufficient traces (good, but duplicates unit test)
- Exempt skill tag enforcement (good, but duplicates unit test)
- Rollback with cooloff (good)

**Missing integration scenarios:**
- **Full optimization lifecycle**: traces collected -> threshold reached -> optimization runs -> skill file updated -> audit log written. This is the most important end-to-end path and it is not tested at integration level.
- **Rollback + re-optimization**: rollback sets cooloff, then after cooloff expires, optimization can proceed again.
- **Concurrent optimization safety**: two skills reaching threshold simultaneously.

---

## Summary

| Category | Files | Current | Target | Gap |
|----------|-------|---------|--------|-----|
| Critical/Safety | guardrails.py | 99% | >= 90% | PASS |
| Audit | candidate_store.py | 96% | >= 90% | PASS |
| Orchestrator | skill_improver_module.py | 64% | >= 90% | **FAIL** (-26%) |
| Business Logic | engine.py | 83% | >= 95% | **BELOW** (-12%) |
| Business Logic | trace_collector.py | 88% | >= 95% | **BELOW** (-7%) |
| Business Logic | evaluator.py | 100% | >= 95% | PASS |
| Business Logic | reflector.py | 100% | >= 95% | PASS |
| Business Logic | pareto.py | 97% | >= 95% | PASS |
| Business Logic | models.py | 99% | >= 95% | PASS |
| Config | config.py | 100% | >= 50% | PASS |

**Top 3 Critical Gaps:**
1. `skill_improver_module.py` at 64% -- the orchestrator's primary activation path (`_on_post_respond` -> `_optimize_skill`) is entirely untested
2. `engine.py` at 83% -- distinct stagnation exit paths (empty reflection, guardrail rejection) are not individually verified
3. `trace_collector.py` at 88% -- error/vetoed tool recording and corrupted file recovery untested

**Overall Verdict**: PASS on aggregate quality gates (89% line, 85% branch). However, the orchestrator module (`skill_improver_module.py`) is significantly below the 90% target for core components and should be addressed before the module is considered production-ready.
