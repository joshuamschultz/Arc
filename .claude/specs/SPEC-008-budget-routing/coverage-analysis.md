# Coverage Analysis: SPEC-008 Budget Control & Compliance-Aware Routing

**Date**: 2026-02-21
**Suite**: 634 tests, 0 failures
**Tool**: `pytest --cov=src/arcllm --cov-branch`

---

## 1. Line & Branch Coverage Per File

| File | Stmts | Miss | Branch | BrPart | Line % | Missing Lines |
|------|-------|------|--------|--------|--------|---------------|
| `exceptions.py` | 24 | 0 | 0 | 0 | **100%** | -- |
| `modules/routing.py` | 45 | 0 | 12 | 0 | **100%** | -- |
| `modules/telemetry.py` | 172 | 5 | 64 | 9 | **94%** | 264, 279-280, 297, 341 |
| `registry.py` | 139 | 30 | 54 | 7 | **76%** | 55, 201-209, 215-246, 283-285, 311 |

### Partial Branches (telemetry.py)

| Line | Branch | Description |
|------|--------|-------------|
| 267->291 | `self._per_call_max is not None` false branch | Per-call max not set, skips pre-flight (tested via `test_no_budget_when_no_limits`) |
| 323->336 | `self._monthly_limit is not None` false branch | Monthly limit absent, skips alert threshold |
| 346->348 | `self._monthly_limit is not None` false branch in `_set_budget_otel` |
| 348->350 | `self._daily_limit is not None` false branch in `_set_budget_otel` |
| 350->352 | `self._per_call_max is not None` false branch in `_set_budget_otel` |

**Lines 264, 297**: Defensive guard branches (`scope is None`, `limit_usd is None`) that are structurally unreachable because `__init__` validation guarantees these can never be None when budget is enabled. These are intentional safety nets; testing them would require monkey-patching internal state.

**Lines 279-280**: The per-call `enforcement="warn"` path (per-call exceeds max but enforcement is warn mode). This is a real gap -- only the "block" path for per-call max is tested.

**Line 341**: `_set_budget_otel` when budget is not enabled. This is implicitly tested (non-budget calls never call this method) but the branch is not explicitly verified.

### Missing Lines (registry.py)

| Lines | Description |
|-------|-------------|
| 55 | `_get_adapter_class` double-check lock fast path (thread safety) |
| 201-209 | Vault resolver construction + vault key resolution for primary provider |
| 215-246 | **Entire routing integration in `load_model()`** -- RoutingModule creation, rule iteration, per-rule provider loading, per-rule vault resolution |
| 283-285 | SecurityModule wiring in `load_model()` |
| 311 | `budget_scope` injection into telemetry config |

---

## 2. Critical Gaps

### P0 -- Must Fix

| # | Gap | File:Lines | Impact | Effort |
|---|-----|-----------|--------|--------|
| 1 | **Routing integration in `load_model()` is untested** | `registry.py:215-246` | The entire code path that creates `RoutingModule` from `load_model(routing={rules: ...})` has zero coverage. This is the glue that wires routing rules to real provider configs, vault keys, and adapter classes. A regression here silently breaks all classification-aware routing. | Medium -- requires mocking `load_provider_config`, adapter classes, and vault |
| 2 | **`budget_scope` injection into telemetry config untested** | `registry.py:311` | The `budget_scope` kwarg on `load_model()` is never tested end-to-end. If this line breaks, budget scoping silently fails at the integration boundary. | Low -- add one test to existing `test_config.py` or `test_registry.py` |
| 3 | **Per-call warn mode untested** | `telemetry.py:279-280` | The warn path when per-call estimated cost exceeds `per_call_max_usd` is not tested. Only `enforcement="block"` is tested for per-call pre-flight. If warn mode silently raises instead of warning, callers would see unexpected exceptions. | Low -- mirror the block test with `enforcement="warn"` |

### P1 -- Should Fix

| # | Gap | File:Lines | Impact | Effort |
|---|-----|-----------|--------|--------|
| 4 | **Vault key resolution for routing rules untested** | `registry.py:233-239` | Per-provider vault key resolution within routing rules is uncovered. In federal environments, each classification route may use a different vault path. A bug here could send authenticated requests to the wrong provider or fail to authenticate at all. | Medium -- requires vault mock |
| 5 | **Alert threshold OTel event not verified** | `telemetry.py:323-334` | The 80% alert threshold fires an OTel span event, but no test verifies the event contents (scope, spend, limit, threshold_pct). Tests verify warn/block behavior but not the early-warning alert. | Low -- add assertion on span events in existing test |
| 6 | **Missing integration test files** | SDD specified `test_budget_telemetry.py` and `test_routing_stack.py` | SDD called for two integration test files that were never created. Budget is tested at unit level (TelemetryModule directly) and routing is tested at unit level (RoutingModule directly), but neither is tested through the full `load_model()` -> module stack path. | Medium |

### P2 -- Nice to Have

| # | Gap | File:Lines | Impact | Effort |
|---|-----|-----------|--------|--------|
| 7 | **OTel span attributes for budget not asserted** | `telemetry.py:338-352` | `_set_budget_otel` sets 7+ span attributes but no test verifies the attribute names/values are correct. The method is called (covered by line coverage) but correctness is not asserted. | Low |
| 8 | **SecurityModule wiring untested** | `registry.py:283-285` | Not SPEC-008 specific but adjacent. SecurityModule integration in `load_model()` has no test. | Low (covered by SPEC-012) |
| 9 | **Routing rule missing provider error path** | `registry.py:222-225` | The `ArcLLMConfigError` when a routing rule lacks a `provider` key is untested. | Trivial |

---

## 3. Requirement Coverage Matrix

### Budget Requirements (FR-B1 through FR-B12)

| ID | Requirement | Test Coverage | Status |
|----|-------------|---------------|--------|
| FR-B1 | Cumulative spend tracking per budget scope | `TestBudgetAccumulator`, `TestBudgetRegistry::test_different_scopes_are_isolated`, `TestAccumulatorIsolation` | COVERED |
| FR-B2 | Three limit types: monthly, daily, per-call | `TestBudgetEnforcement::test_block_mode_raises_when_monthly_exceeded`, `test_block_mode_raises_when_daily_exceeded`, `test_per_call_max_blocks` | COVERED |
| FR-B3 | Calendar boundary resets (UTC) | `TestBudgetPeriodBoundary::test_monthly_reset_on_new_month`, `test_daily_reset_on_new_day` | COVERED |
| FR-B4 | Block mode raises ArcLLMBudgetError | `TestBudgetEnforcement::test_block_mode_raises_when_monthly_exceeded`, `test_block_mode_raises_when_daily_exceeded`, `test_per_call_max_blocks` | COVERED |
| FR-B5 | Warn mode allows + metadata annotation | `TestBudgetEnforcement::test_warn_mode_allows_when_exceeded` (monthly/daily only) | **PARTIAL** -- per-call warn not tested |
| FR-B6 | Alert threshold OTel event | No test verifies the OTel event content | **GAP** |
| FR-B7 | Pre-flight estimate blocks excessive calls | `TestBudgetEnforcement::test_per_call_max_blocks` | COVERED (block only) |
| FR-B8 | `budget_scope` as `load_model()` kwarg | Config injection at `registry.py:311` is untested | **GAP** |
| FR-B9 | Scope validation (NFKC, regex, 128 chars) | `TestBudgetScopeValidation` (11 tests), `TestScopeInjection` (6 tests) | COVERED |
| FR-B10 | Cost clamped to max(0.0, cost) | `TestBudgetCostClamping`, `TestNegativeCostInjection` | COVERED |
| FR-B11 | OTel span attributes under `arcllm.budget.*` | `_set_budget_otel` is executed but attributes not asserted | **PARTIAL** |
| FR-B12 | Budget config under `[modules.telemetry]` | `test_config.py` loads config; budget fields are validated in `TestBudgetValidation` | COVERED |

### Routing Requirements (FR-R1 through FR-R10)

| ID | Requirement | Test Coverage | Status |
|----|-------------|---------------|--------|
| FR-R1 | RoutingModule implements LLMProvider | `TestRoutingSelection`, all invoke tests | COVERED |
| FR-R2 | Classification->provider mapping via TOML config | `test_config.py:45-46` validates parsing; `TestRoutingSelection` validates runtime | COVERED |
| FR-R3 | Classification kwarg consumed (popped) by RoutingModule | `TestRoutingSelection::test_classification_popped_from_kwargs` | COVERED |
| FR-R4 | Eager adapter loading at init (fail-fast) | `TestRoutingValidation::test_empty_adapters_rejected`, `test_default_classification_must_exist_in_adapters` | COVERED |
| FR-R5 | Unknown classification + warn = default route | `TestRoutingUnknown::test_unknown_classification_warn_defaults` | COVERED |
| FR-R6 | Unknown classification + block = error | `TestRoutingUnknown::test_unknown_classification_block_raises` | COVERED |
| FR-R7 | `close()` closes all internal adapters | `TestRoutingAdapterLifecycle::test_close_closes_all_adapters` | COVERED |
| FR-R8 | `routing` kwarg on `load_model()` | `registry.py:213-254` -- routing integration in load_model is UNCOVERED | **GAP** |
| FR-R9 | Per-provider vault key resolution | `registry.py:233-239` is uncovered | **GAP** |
| FR-R10 | `name`/`model_name` from default adapter | `TestRoutingProperties` (2 tests) | COVERED |

---

## 4. Security Test Coverage

| Attack Category | Test Class | Tests | Status |
|-----------------|-----------|-------|--------|
| Scope Injection | `TestScopeInjection` | 6 tests (SQL, path traversal, null byte, Cyrillic, fullwidth, newline) | COVERED |
| Negative Cost Injection | `TestNegativeCostInjection` | 2 tests (negative tokens, multiple negative calls) | COVERED |
| Classification Downgrade | `TestClassificationDowngrade` | 2 tests (block mode, case sensitivity) | COVERED |
| Adapter Isolation | `TestAdapterIsolation` | 2 tests (distinct instances, independent close) | COVERED |
| Accumulator Isolation | `TestAccumulatorIsolation` | 1 test (cross-scope spend leakage) | COVERED |
| Float Overflow | `TestFloatOverflow` | 1 test (2^53 tokens) | COVERED |

All 6 attack categories from the PRD's success criteria (item 11) are covered.

Additionally, `TestConfigInjection` (2 tests) and `TestRoutingAuditTrail` (1 test) provide extra defense-in-depth coverage.

---

## 5. Quality Gate Verdict

| Gate | Threshold | Actual | Verdict |
|------|-----------|--------|---------|
| **exceptions.py** line coverage | >= 90% | **100%** | PASS |
| **modules/routing.py** line coverage | >= 90% | **100%** | PASS |
| **modules/telemetry.py** line coverage | >= 90% | **94%** | PASS |
| **registry.py** line coverage | >= 80% | **76%** | **FAIL** |
| Overall project line coverage | >= 80% | **94%** | PASS |
| Overall project branch coverage | >= 75% | ~91% | PASS |
| Security test categories | 6 | 6 | PASS |
| All tests pass | 0 failures | 634 passed | PASS |
| NFR-7 (new code >= 90%) | >= 90% | ~90% (excl. registry integration) | BORDERLINE |

### Overall: CONDITIONAL PASS

Three of four SPEC-008 implementation files exceed 90% coverage. `registry.py` is at 76% overall, but the miss is concentrated in the routing integration path (lines 215-246) which is entirely SPEC-008 code with zero coverage. This means the integration wiring -- the code that actually creates a `RoutingModule` from `load_model()` -- has never been exercised by any test.

---

## 6. Recommended Improvements (Prioritized)

### Phase 1: P0 Fixes (Critical -- blocks quality gate)

1. **Test `load_model(routing=...)` integration** -- Write tests that call `load_model()` with routing rules dict, verify RoutingModule is returned, verify adapters are created per-rule. Mock `load_provider_config` and adapter classes.
   - Expected coverage increase: registry.py 76% -> ~88%

2. **Test `budget_scope` injection** -- Call `load_model(budget_scope="agent:test", telemetry={...})` and verify scope propagates to TelemetryModule.
   - Expected coverage increase: registry.py +1 line

3. **Test per-call warn mode** -- Mirror `test_per_call_max_blocks` but with `enforcement="warn"`, verify `response.metadata["budget_warning"]` is True.
   - Expected coverage increase: telemetry.py 94% -> 95%

### Phase 2: P1 Fixes (High impact)

4. **Assert alert threshold OTel event** -- In an existing budget test, verify that the `budget_alert` span event is emitted when spend crosses the threshold.

5. **Test vault resolution in routing rules** -- With vault mock, verify per-rule API key resolution.

### Phase 3: P2 Fixes (Nice to have)

6. **Assert `_set_budget_otel` attribute names/values** -- Verify the 7 span attributes.
7. **Test routing rule missing provider error** -- Trivial error path test.
