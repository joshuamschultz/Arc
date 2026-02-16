# PLAN: Telemetry Module

> Implementation tasks for ArcLLM Step 10.
> Status: COMPLETE

---

## Progress

**Completed**: 6/6 tasks
**Remaining**: 0 tasks

---

## Phase 1: Config Update (Task 1)

### T10.1 Update config.toml with telemetry log_level `[activity: configuration]`

Add `log_level = "INFO"` to `[modules.telemetry]` section.

- [x] Edit `src/arcllm/config.toml` — add `log_level = "INFO"` to `[modules.telemetry]`
- [x] Verify `ModuleConfig` accepts the new field (uses `extra="allow"`)

**Verify**: `pytest -v` — all 245 existing tests still pass

---

## Phase 2: TDD RED — Write Tests First (Task 2)

### T10.2 Write test_telemetry.py — tests first (TDD RED) `[activity: unit-testing]`

Write comprehensive tests before implementing. All tests should fail with ImportError.

- [x] Create `tests/test_telemetry.py`
- [x] **TestTelemetryModule** (12 tests):
  - [x] `test_invoke_delegates_to_inner` — messages passed, response returned
  - [x] `test_invoke_passes_tools_and_kwargs` — tools, max_tokens forwarded
  - [x] `test_logs_timing_and_usage` — mock time.monotonic, verify all fields in log
  - [x] `test_logs_cost_calculation` — verify cost math (100 input * 3.00/1M + 50 output * 15.00/1M)
  - [x] `test_logs_cache_tokens_when_present` — cache_read_tokens/cache_write_tokens in log
  - [x] `test_no_cache_fields_when_absent` — omitted from log when None
  - [x] `test_cost_includes_cache_tokens` — full cost with all 4 token types
  - [x] `test_cost_zero_when_zero_tokens` — cost_usd=0.000000
  - [x] `test_custom_log_level` — DEBUG not visible at INFO, visible at DEBUG
  - [x] `test_provider_name_from_inner` — module.name == inner.name
  - [x] `test_model_name_from_inner` — module.model_name == inner.model_name
  - [x] `test_returns_response_unchanged` — same object reference (is check)
- [x] **TestTelemetryValidation** (4 tests):
  - [x] `test_negative_cost_input_rejected` — ArcLLMConfigError
  - [x] `test_negative_cost_output_rejected` — ArcLLMConfigError
  - [x] `test_missing_cost_fields_default_to_zero` — all 4 default to 0.0
  - [x] `test_invalid_log_level_rejected` — ArcLLMConfigError
- [x] **TestTelemetryCostCalculation** (5 tests):
  - [x] `test_basic_cost_no_cache` — input + output only
  - [x] `test_cost_with_cache_read` — adds cache read cost
  - [x] `test_cost_with_all_token_types` — all 4 token types
  - [x] `test_cost_zero_when_no_pricing` — empty config = 0.0
  - [x] `test_cost_with_million_tokens` — exact cost at 1M boundary
- [x] Run tests — all FAIL (RED confirmed: ImportError)

**Verify**: Tests written, all fail with ImportError (correct RED)

---

## Phase 3: Implementation (Task 3)

### T10.3 Implement TelemetryModule in modules/telemetry.py `[activity: backend-development]`

- [x] Create `src/arcllm/modules/telemetry.py`
- [x] Implement `TelemetryModule(BaseModule)`:
  - [x] `__init__(config, inner)` — extract pricing from config, validate costs >= 0, validate log_level
  - [x] `_calculate_cost(usage) -> float` — (input * cost + output * cost) / 1M + conditional cache costs
  - [x] `invoke(messages, tools, **kwargs)` — time.monotonic start/end, calculate cost, build log parts, conditional cache fields, logger.log()
- [x] Logger: `logging.getLogger(__name__)` — logger name: `arcllm.modules.telemetry`
- [x] Valid log levels: `{"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}`
- [x] Run all telemetry tests — GREEN (21/21 passing)

**Verify**: `pytest tests/test_telemetry.py -v` — all 21 pass

---

## Phase 4: Registry Integration (Task 4)

### T10.4 Update registry.py with telemetry kwarg and pricing injection `[activity: backend-development]`

- [x] Update `load_model()` signature — add `telemetry: bool | dict | None = None`
- [x] Add telemetry stacking (outermost, after retry):
  - [x] Resolve telemetry config via `_resolve_module_config("telemetry", telemetry)`
  - [x] Inject pricing from `config.models.get(model_name)` via `setdefault()`
  - [x] Wrap: `result = TelemetryModule(telemetry_config, result)`
- [x] Update docstring: stacking order `Telemetry -> Retry -> Fallback -> RateLimit -> Adapter`
- [x] Add registry integration tests to `test_registry.py`:
  - [x] `test_load_model_with_telemetry` — wraps with TelemetryModule
  - [x] `test_load_model_telemetry_injects_pricing` — cost rates from anthropic.toml
  - [x] `test_load_model_telemetry_custom_model_pricing` — haiku pricing differs from sonnet
  - [x] `test_load_model_telemetry_dict_overrides_pricing` — explicit cost overrides metadata
  - [x] `test_load_model_full_stack_with_telemetry` — Telemetry(Retry(Fallback(RateLimit(adapter))))
  - [x] `test_load_model_telemetry_false_overrides_config` — kwarg disables config
  - [x] Renamed `test_load_model_full_stack_order` to `test_load_model_full_stack_order_without_telemetry`

**Verify**: `pytest tests/test_registry.py -v` — all pass

---

## Phase 5: Export Updates (Task 5)

### T10.5 Update __init__.py and modules/__init__.py exports `[activity: backend-development]`

- [x] Update `src/arcllm/modules/__init__.py` — add `TelemetryModule` import and `__all__`
- [x] Update `src/arcllm/__init__.py` — add `TelemetryModule` to `_LAZY_IMPORTS` and `__all__`

**Verify**: `from arcllm import TelemetryModule` works

---

## Phase 6: Full Verification (Task 6)

### T10.6 Full test suite verification `[activity: run-tests]`

- [x] Run `pytest -v` — ALL tests pass (245 existing + 28 new = 272 passed, 1 skipped)
- [x] Verify existing tests unaffected — zero regressions
- [x] Count total tests: 272 passed, 1 skipped
- [x] Quick smoke test: `from arcllm import TelemetryModule` works

**Verify**: Full suite green

---

## Acceptance Criteria

- [x] `TelemetryModule` wraps invoke() and logs structured metrics after each call
- [x] Wall-clock duration measured via `time.monotonic()`
- [x] `duration_ms` field rounded to 1 decimal
- [x] Token usage from `LLMResponse.usage` logged (input, output, total)
- [x] Cache tokens conditionally logged when not None
- [x] Cache token fields omitted when None
- [x] Cost calculated: `(tokens * cost_per_1m) / 1_000_000`
- [x] Cost includes all 4 token types when present
- [x] Cost defaults to 0.0 when no pricing configured
- [x] Pricing injected by `load_model()` from provider model metadata
- [x] `setdefault()` injection: explicit overrides take precedence
- [x] Configurable log level (default INFO)
- [x] Invalid log level raises `ArcLLMConfigError`
- [x] Negative costs raise `ArcLLMConfigError`
- [x] `load_model("x", telemetry=True)` enables telemetry
- [x] `load_model("x", telemetry=False)` disables even if config enabled
- [x] `load_model("x", telemetry={"log_level": "DEBUG"})` overrides config
- [x] Stacking order: Telemetry(Retry(Fallback(RateLimit(adapter))))
- [x] Agent code unchanged — `model.invoke()` just works
- [x] All existing 245 tests pass unchanged
- [x] 21 new telemetry tests + 7 new registry tests — all passing
- [x] New tests fully mocked (no real API calls, no real time waits)
- [x] Zero new dependencies
- [x] Total: 272 passed, 1 skipped
