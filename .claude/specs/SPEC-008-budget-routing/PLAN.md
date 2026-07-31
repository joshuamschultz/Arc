# Implementation Plan: Budget Control & Compliance-Aware Routing

**Spec**: SPEC-008 | **Status**: COMPLETE | **Total tasks**: 22 | **Completed**: 22 | **Remaining**: 0

## What We're NOT Doing

- Per-provider budget tracking (use OTel/Grafana queries)
- Budget persistence across restarts (OTel collectors are durable)
- Content-based classification detection (Step 18)
- Hot-reload of routing/budget config
- Billing reconciliation

## Specification References

- **PRD**: `.claude/specs/SPEC-008-budget-routing/PRD.md`
- **SDD**: `.claude/specs/SPEC-008-budget-routing/SDD.md`
- **Build Decisions**: `.claude/decisions-log.md` (ArcLLM Budget Control section)
- **Quality**: `mypy --strict`, `ruff check`, `pytest --cov >= 90%`

---

## Phase 1: Foundation (Exceptions + Accumulator)

**Goal**: Standalone budget components with no module changes yet.

- [x] **1.1 Add ArcLLMBudgetError to exception hierarchy**
  - File: `packages/arcllm/src/arcllm/exceptions.py`
  - Implementation:
    - Add `ArcLLMBudgetError(ArcLLMError)` with `scope`, `limit_type`, `limit_usd`, `current_usd`, `estimated_usd` attributes
    - Follow existing `ArcLLMAPIError` pattern for attribute storage
  - Purpose: Runtime exception for budget limit violations
  - _Leverage: `ArcLLMAPIError` in same file_
  - _Requirements: FR-B4_

- [x] **1.2 Write tests for ArcLLMBudgetError**
  - File: `packages/arcllm/tests/test_budget.py`
  - Implementation:
    - Test construction with all fields
    - Test string representation includes scope, limit_type, amounts
    - Test inheritance from `ArcLLMError`
  - Purpose: Verify exception contract before use
  - _Requirements: FR-B4_

- [x] **1.3 Create BudgetAccumulator with period tracking**
  - File: `packages/arcllm/src/arcllm/modules/telemetry.py` (add class above TelemetryModule)
  - Implementation:
    - `BudgetAccumulator` class with `monthly_spend`, `daily_spend`, `current_month` (YYYYMM int), `current_day` (YYYYMMDD int)
    - `_maybe_reset()` — auto-reset on UTC calendar boundaries
    - `deduct(cost: float)` — add clamped cost to accumulators
    - `check_limits(monthly_limit, daily_limit)` — return exceeded limit type or None
    - `check_pre_flight(estimated, per_call_max)` — return True if estimated exceeds max
  - Purpose: Core budget enforcement logic, independent of module
  - _Leverage: `TokenBucket` class in `rate_limit.py`_
  - _Requirements: FR-B1, FR-B2, FR-B3, FR-B7, FR-B10_

- [x] **1.4 Write tests for BudgetAccumulator**
  - File: `packages/arcllm/tests/test_budget.py`
  - Implementation:
    - `TestBudgetAccumulator`: deduct, monthly/daily limits, period reset, float precision
    - `TestBudgetPreFlight`: per-call estimate check
    - `TestBudgetPeriodBoundary`: mock UTC time, verify month/day resets
    - `TestBudgetCostClamping`: negative cost clamped to 0.0
  - Purpose: Full coverage of accumulator logic before integration
  - _Requirements: FR-B1, FR-B2, FR-B3, FR-B7, FR-B10_

- [x] **1.5 Add budget scope validation function**
  - File: `packages/arcllm/src/arcllm/modules/telemetry.py`
  - Implementation:
    - `_validate_budget_scope(scope: str) -> None` with NFKC normalization + regex `^[a-z][a-z0-9_:.\-]{0,127}$`
    - Raise `ArcLLMConfigError` on invalid scope
  - Purpose: Prevent scope string injection
  - _Leverage: `_validate_provider_name()` in `config.py`_
  - _Requirements: FR-B9_

- [x] **1.6 Write tests for scope validation**
  - File: `packages/arcllm/tests/test_budget.py`
  - Implementation:
    - Valid scopes: `agent:agent-007`, `agent:test.scope`, `a`
    - Invalid: empty, uppercase, spaces, unicode homoglyphs, path traversal, SQL injection, >128 chars
  - Purpose: Security boundary validation
  - _Requirements: FR-B9_

- [x] **1.7 Add budget accumulator registry + clear function**
  - File: `packages/arcllm/src/arcllm/modules/telemetry.py`
  - Implementation:
    - `_budget_registry: dict[str, BudgetAccumulator] = {}`
    - `_get_or_create_accumulator(scope: str) -> BudgetAccumulator`
    - `clear_budgets() -> None` for test isolation
  - Purpose: Shared state management following rate_limit.py pattern
  - _Leverage: `_bucket_registry` pattern in `rate_limit.py`_
  - _Requirements: FR-B1_

- [x] **1.8 Register clear_budgets() in registry.clear_cache()**
  - File: `packages/arcllm/src/arcllm/registry.py`
  - Implementation:
    - Import `clear_budgets` from `arcllm.modules.telemetry`
    - Call `clear_budgets()` in `clear_cache()` alongside existing `clear_buckets()` and `reset_sdk()`
  - Purpose: Test isolation — prevent budget state leaking between tests
  - _Leverage: Existing `clear_buckets()` call pattern in `registry.py:29-35`_
  - _Requirements: NFR-4_

**PAUSE POINT**: Run `pytest packages/arcllm/tests/test_budget.py` + `mypy --strict` + `ruff check`. All Phase 1 tests must pass before proceeding.

---

## Phase 2: Budget Integration into TelemetryModule

**Goal**: TelemetryModule enforces budget limits on every invoke().

- [x] **2.1 Extend TelemetryModule config keys for budget**
  - File: `packages/arcllm/src/arcllm/modules/telemetry.py`
  - Implementation:
    - Add to `_VALID_CONFIG_KEYS`: `monthly_limit_usd`, `daily_limit_usd`, `per_call_max_usd`, `alert_threshold_pct`, `enforcement`
    - Validate `enforcement` in `{"warn", "block"}` in constructor
    - Validate all budget limits `>= 0` using existing cost field pattern
    - Store as instance attributes
  - Purpose: Budget config validation at construction time
  - _Leverage: Existing cost field validation pattern in `__init__`_
  - _Requirements: FR-B12_

- [x] **2.2 Add budget enforcement to TelemetryModule.invoke()**
  - File: `packages/arcllm/src/arcllm/modules/telemetry.py`
  - Implementation:
    - Accept `budget_scope` from constructor config (passed via load_model)
    - Pre-call: check pre-flight estimate, check cumulative limits
    - If block mode: raise `ArcLLMBudgetError`
    - If warn mode: set `response.metadata["budget_warning"] = True`
    - Post-call: deduct `max(0.0, cost)` from accumulator
    - Set OTel span attributes under `arcllm.budget.*` namespace
    - Emit alert event at threshold crossing
  - Purpose: Core budget enforcement integrated into telemetry flow
  - _Requirements: FR-B1, FR-B4, FR-B5, FR-B6, FR-B7, FR-B10, FR-B11_

- [x] **2.3 Write budget enforcement tests**
  - File: `packages/arcllm/tests/test_budget.py`
  - Implementation:
    - `TestBudgetEnforcement`: block mode raises error, warn mode sets metadata
    - `TestBudgetAlertThreshold`: OTel event at 80% spend
    - `TestBudgetOtelAttributes`: verify all span attributes set correctly
    - `TestBudgetValidation`: invalid enforcement rejected, negative limits rejected
  - Purpose: Full enforcement behavior coverage
  - _Requirements: FR-B4, FR-B5, FR-B6, FR-B11_

- [x] **2.4 Update config.toml with budget fields**
  - File: `packages/arcllm/src/arcllm/config.toml`
  - Implementation:
    - Move budget fields from `[modules.budget]` to `[modules.telemetry]`
    - Add: `monthly_limit_usd`, `daily_limit_usd`, `per_call_max_usd`, `alert_threshold_pct`, `enforcement`
    - Remove old `[modules.budget]` section
  - Purpose: Budget config lives with telemetry
  - _Requirements: FR-B12_

- [x] **2.5 Add budget_scope kwarg to load_model()**
  - File: `packages/arcllm/src/arcllm/registry.py`
  - Implementation:
    - Add `budget_scope: str | None = None` parameter
    - When telemetry is enabled and has budget fields, validate `budget_scope` is provided
    - Inject `budget_scope` into telemetry config dict
    - Raise `ArcLLMConfigError` if budget enabled but no scope provided
  - Purpose: API surface for budget scope
  - _Leverage: Existing kwarg handling in `load_model()`_
  - _Requirements: FR-B8_

- [x] **2.6 Write integration test for budget in telemetry stack**
  - File: `packages/arcllm/tests/test_budget_telemetry.py`
  - Implementation:
    - Full `load_model()` → `invoke()` flow with budget enabled
    - Verify budget blocks at limit
    - Verify budget warns at limit
    - Verify existing telemetry behavior unchanged when no budget config
    - Verify scope required when budget config present
  - Purpose: End-to-end budget integration
  - _Requirements: FR-B1 through FR-B12_

**PAUSE POINT**: Run full test suite: `pytest packages/arcllm/tests/` + `mypy --strict` + `ruff check`. All existing + new tests must pass. Zero regressions.

---

## Phase 3: Routing Module

**Goal**: RoutingModule routes by classification, replaces adapter at innermost position.

- [x] **3.1 Create RoutingModule implementing LLMProvider**
  - File: `packages/arcllm/src/arcllm/modules/routing.py` (NEW)
  - Implementation:
    - `RoutingModule(LLMProvider)` with `__init__(config: dict)` that eagerly loads all adapters
    - `name` and `model_name` properties from default route
    - `invoke()` pops `classification` kwarg, looks up adapter, delegates
    - `validate_config()` checks all adapters valid
    - `close()` closes all adapters
    - Validation: enforcement in `{"warn", "block"}`, default_classification must exist in rules
    - OTel span with `arcllm.routing.*` attributes
  - Purpose: Classification-based provider routing
  - _Leverage: `BaseModule` pattern, `_get_adapter_class()` in registry.py_
  - _Requirements: FR-R1, FR-R2, FR-R3, FR-R4, FR-R5, FR-R6, FR-R7, FR-R9, FR-R10_

- [x] **3.2 Write tests for RoutingModule**
  - File: `packages/arcllm/tests/test_routing.py`
  - Implementation:
    - `TestRoutingSelection`: classification -> correct adapter
    - `TestRoutingUnknown`: warn mode defaults, block mode raises
    - `TestRoutingAdapterLifecycle`: eager init, close all, validate_config
    - `TestRoutingValidation`: invalid config rejected
    - `TestRoutingKwargsFlow`: classification popped, remaining kwargs passed through
    - `TestRoutingOtel`: span attributes set correctly
  - Purpose: Full routing behavior coverage
  - _Requirements: FR-R1 through FR-R10_

- [x] **3.3 Add routing kwarg to load_model()**
  - File: `packages/arcllm/src/arcllm/registry.py`
  - Implementation:
    - Add `routing: bool | dict[str, Any] | None = None` parameter
    - When routing enabled: create `RoutingModule` instead of single adapter
    - Router replaces adapter BEFORE module wrapping (innermost position)
    - Normal module stack wraps Router just like it wraps adapter
  - Purpose: Wire Router into the module stack
  - _Leverage: Existing module kwarg pattern in `load_model()`_
  - _Requirements: FR-R1, FR-R8_

- [x] **3.4 Update config.toml with routing section**
  - File: `packages/arcllm/src/arcllm/config.toml`
  - Implementation:
    - Add `enforcement`, `default_classification` to `[modules.routing]`
    - Add example rule sections (commented out): `[modules.routing.rules.cui]`, `[modules.routing.rules.unclassified]`
  - Purpose: Default routing configuration
  - _Requirements: FR-R2_

- [x] **3.5 Write integration test for routing in full stack**
  - File: `packages/arcllm/tests/test_routing_stack.py`
  - Implementation:
    - Router with full module wrapping (otel, telemetry, audit, etc.)
    - Verify classification flows through all modules to Router
    - Verify Router selects correct adapter
    - Verify OTel spans contain routing attributes
    - Verify budget + routing work together
  - Purpose: End-to-end routing integration
  - _Requirements: FR-R1 through FR-R10_

**PAUSE POINT**: Run full test suite: `pytest packages/arcllm/tests/` + `mypy --strict` + `ruff check`. All tests pass. Zero regressions.

---

## Phase 4: Security Tests

**Goal**: Adversarial testing for both budget and routing.

- [x] **4.1 Write budget security tests** [parallel: true]
  - File: `packages/arcllm/tests/security/test_budget_security.py`
  - Implementation:
    - Scope injection: SQL-like strings, path traversal, unicode homoglyphs
    - Negative cost injection: adapter returns negative tokens
    - Config override: attempt to lower limits via kwarg
    - Accumulator isolation: verify per-scope independence
    - Float overflow: massive token counts
  - Purpose: Budget bypass prevention
  - _Requirements: FR-B9, FR-B10, NFR-8_

- [x] **4.2 Write routing security tests** [parallel: true]
  - File: `packages/arcllm/tests/security/test_routing_security.py`
  - Implementation:
    - Classification downgrade: attempt to route CUI to unclassified provider
    - Adapter isolation: verify adapters don't share state
    - Config injection: attempt to modify rules at runtime
    - Audit trail: verify all routing decisions logged
  - Purpose: Routing bypass prevention
  - _Requirements: FR-R5, FR-R6_

**PAUSE POINT**: Run `pytest packages/arcllm/tests/security/` — all security tests pass.

---

## Phase 5: Final Validation

**Goal**: Full quality gate pass.

- [x] **5.1 Run full test suite and verify zero regressions**
  - Command: `cd packages/arcllm && pytest tests/ --cov=src/arcllm -v`
  - Purpose: Verify all existing + new tests pass
  - _Requirements: NFR-4, NFR-7_

- [x] **5.2 Run mypy strict type checking**
  - Command: `cd packages/arcllm && mypy src/arcllm/ --strict`
  - Purpose: Verify type safety
  - _Requirements: NFR-5_

- [x] **5.3 Run ruff linting**
  - Command: `cd packages/arcllm && ruff check src/arcllm/ tests/`
  - Purpose: Verify code quality
  - _Requirements: NFR-6_

---

## Success Criteria

### Automated
- [ ] `pytest tests/` — all pass, 0 failures
- [ ] `pytest --cov` — new code >= 90% coverage
- [ ] `mypy --strict` — 0 errors
- [ ] `ruff check` — 0 errors
- [ ] Security tests — all 6 attack categories covered and passing

### Manual
- [ ] Review: budget enforcement blocks in block mode, warns in warn mode
- [ ] Review: routing selects correct adapter per classification
- [ ] Review: existing module behavior unchanged when budget/routing not configured
- [ ] Review: OTel spans contain all budget and routing attributes

---

## Requirement Traceability Matrix

| Requirement | Task(s) | Test(s) |
|-------------|---------|---------|
| FR-B1 (accumulator) | 1.3, 1.7, 2.2 | 1.4, 2.3, 2.6 |
| FR-B2 (three limits) | 1.3 | 1.4 |
| FR-B3 (auto-reset) | 1.3 | 1.4 |
| FR-B4 (block mode) | 1.1, 2.2 | 1.2, 2.3, 2.6 |
| FR-B5 (warn mode) | 2.2 | 2.3, 2.6 |
| FR-B6 (alert threshold) | 2.2 | 2.3 |
| FR-B7 (pre-flight) | 1.3, 2.2 | 1.4, 2.3 |
| FR-B8 (budget_scope kwarg) | 2.5 | 2.6 |
| FR-B9 (scope validation) | 1.5 | 1.6, 4.1 |
| FR-B10 (cost clamping) | 1.3, 2.2 | 1.4, 4.1 |
| FR-B11 (OTel attributes) | 2.2 | 2.3 |
| FR-B12 (config under telemetry) | 2.1, 2.4 | 2.3 |
| FR-R1 (RoutingModule) | 3.1 | 3.2, 3.5 |
| FR-R2 (rules mapping) | 3.1, 3.4 | 3.2 |
| FR-R3 (classification kwarg) | 3.1 | 3.2, 3.5 |
| FR-R4 (eager loading) | 3.1 | 3.2 |
| FR-R5 (unknown warn) | 3.1 | 3.2 |
| FR-R6 (unknown block) | 3.1 | 3.2, 4.2 |
| FR-R7 (close all) | 3.1 | 3.2 |
| FR-R8 (routing kwarg) | 3.3 | 3.5 |
| FR-R9 (vault per-provider) | 3.1 | 3.2 |
| FR-R10 (name/model_name) | 3.1 | 3.2 |
