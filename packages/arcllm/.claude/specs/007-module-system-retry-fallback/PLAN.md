# PLAN: Module System + Retry + Fallback

> Implementation tasks for ArcLLM Step 7.
> Status: COMPLETE

---

## Progress

**Completed**: 8/8 tasks
**Remaining**: 0 tasks

---

## Phase 1: Foundation — BaseModule + Exception Update (Tasks 1-2)

### T7.1 Update ArcLLMAPIError with status_code `[activity: backend-development]`

Check current `ArcLLMAPIError` — if it doesn't have `status_code`, add it. RetryModule needs this to detect retryable HTTP errors.

- [x] Read `src/arcllm/exceptions.py` — check ArcLLMAPIError fields
- [x] If needed: add `status_code: int | None = None` and `retry_after: float | None = None`
- [x] Update adapters to pass `status_code` when raising ArcLLMAPIError
- [x] Run existing tests — zero regressions

**Result**: ArcLLMAPIError already had `status_code` field — no changes needed.

**Verify**: `pytest -v` — all 149 existing tests pass

### T7.2 Create BaseModule in modules/base.py `[activity: backend-development]`

Create the module foundation class that all modules inherit from.

- [x] Create `src/arcllm/modules/__init__.py`
- [x] Create `src/arcllm/modules/base.py` with `BaseModule` class:
  - [x] Implements `LLMProvider` interface
  - [x] `__init__(self, config: dict, inner: LLMProvider)` — stores config and inner reference
  - [x] `name` property — delegates to `inner.name`
  - [x] `model_name` property — delegates to `inner.model_name`
  - [x] `invoke()` — delegates to `inner.invoke()`
  - [x] `validate_config()` — delegates to `inner.validate_config()`
- [x] Create `tests/test_module_base.py` (TDD — write tests first):
  - [x] `test_base_module_delegates_invoke` — calls inner.invoke()
  - [x] `test_base_module_delegates_name` — returns inner.name
  - [x] `test_base_module_delegates_validate_config` — returns inner.validate_config()
  - [x] `test_base_module_is_llm_provider` — isinstance check
  - [x] `test_base_module_transparent_wrapper` — response unchanged
- [x] Run tests — GREEN (10/10)

**Verify**: `pytest tests/test_module_base.py -v` — all pass

---

## Phase 2: RetryModule (Tasks 3-4)

### T7.3 Write test_retry.py — tests first (TDD RED) `[activity: unit-testing]`

Write comprehensive tests before implementing RetryModule.

- [x] Create `tests/test_retry.py`
- [x] **TestRetrySuccess**:
  - [x] `test_first_try_succeeds_no_retry` — inner succeeds, no retry
  - [x] `test_retry_on_429_then_succeed` — fail once with 429, succeed on retry
  - [x] `test_retry_on_500_then_succeed` — fail once with 500, succeed on retry
  - [x] `test_retry_on_502_then_succeed`
  - [x] `test_retry_on_503_then_succeed`
  - [x] `test_retry_on_529_then_succeed` — Anthropic-specific overload
  - [x] `test_retry_on_connection_error` — httpx.ConnectError, then succeed
  - [x] `test_retry_on_timeout_error` — httpx.TimeoutException, then succeed
- [x] **TestRetryExhaustion**:
  - [x] `test_max_retries_exceeded_raises` — fails max_retries+1 times, raises last error
  - [x] `test_raises_original_error_type` — preserves exception type
- [x] **TestRetryPassthrough**:
  - [x] `test_no_retry_on_400` — bad request not retried
  - [x] `test_no_retry_on_401` — auth error not retried
  - [x] `test_no_retry_on_403` — forbidden not retried
  - [x] `test_no_retry_on_non_api_error` — ValueError etc. pass through
- [x] **TestRetryBackoff**:
  - [x] `test_backoff_increases_exponentially` — mock asyncio.sleep, verify wait times
  - [x] `test_backoff_capped_at_max_wait` — doesn't exceed max_wait_seconds
  - [x] `test_jitter_applied` — wait time has random component
- [x] **TestRetryConfig**:
  - [x] `test_custom_max_retries` — respects configured value
  - [x] `test_custom_retry_codes` — only retries configured codes
- [x] Run tests — all FAIL (RED confirmed)

**Verify**: Tests written, all failed with ImportError (correct RED)

### T7.4 Implement RetryModule in modules/retry.py `[activity: backend-development]`

- [x] Create `src/arcllm/modules/retry.py`:
  - [x] Config parsed from dict (no separate pydantic model — kept simple)
  - [x] `RetryModule(BaseModule)` class
  - [x] `__init__(self, config: dict, inner: LLMProvider)` — parses config with defaults
  - [x] `invoke()` override with retry loop
  - [x] `_is_retryable(error)` — checks status code and exception type
  - [x] `_calculate_wait(attempt)` — exponential backoff + jitter
- [x] Run tests — GREEN (19/19)

**Verify**: `pytest tests/test_retry.py -v` — all pass

---

## Phase 3: FallbackModule (Tasks 5-6)

### T7.5 Write test_fallback.py — tests first (TDD RED) `[activity: unit-testing]`

- [x] Create `tests/test_fallback.py`
- [x] **TestFallbackSuccess**:
  - [x] `test_primary_succeeds_no_fallback` — inner works, chain not touched
  - [x] `test_primary_fails_first_fallback_succeeds` — inner fails, first chain entry works
  - [x] `test_primary_fails_second_fallback_succeeds` — first fallback also fails, second works
- [x] **TestFallbackExhaustion**:
  - [x] `test_all_fallbacks_fail_raises_primary_error` — chain exhausted, original error raised
  - [x] `test_empty_chain_passes_through` — no chain configured, error passes through
- [x] **TestFallbackCreation**:
  - [x] `test_fallback_adapter_created_via_load_model` — mock load_model, verify called with chain provider name
  - [x] `test_fallback_adapter_created_on_demand` — load_model not called until failure
- [x] Run tests — all FAIL (RED confirmed)

**Verify**: Tests written, all failed with ImportError (correct RED)

### T7.6 Implement FallbackModule in modules/fallback.py `[activity: backend-development]`

- [x] Create `src/arcllm/modules/fallback.py`:
  - [x] Config parsed from dict (chain: list[str])
  - [x] `FallbackModule(BaseModule)` class
  - [x] `__init__(self, config: dict, inner: LLMProvider)` — parses chain from config
  - [x] `invoke()` override with try/except and chain walking
  - [x] Uses lazy-imported `load_model()` from registry (avoids circular import)
- [x] Run tests — GREEN (7/7)

**Verify**: `pytest tests/test_fallback.py -v` — all pass

---

## Phase 4: Registry Integration (Tasks 7)

### T7.7 Update registry.py for module stacking `[activity: backend-development]`

- [x] Add `_resolve_module_config()` helper — merges config.toml settings with load_model() kwargs
- [x] Update `load_model()`:
  - [x] After adapter construction, load global config (cached)
  - [x] Check retry config — if enabled, wrap with RetryModule
  - [x] Check fallback config — if enabled, wrap with FallbackModule
  - [x] Stacking order: Retry(Fallback(adapter))
- [x] Handle kwarg formats: `retry=True`, `retry=False`, `retry={"max_retries": 5}`
- [x] Add module integration tests to `test_registry.py`:
  - [x] `test_load_model_with_retry_kwarg` — wraps adapter with RetryModule
  - [x] `test_load_model_with_retry_dict` — custom config dict
  - [x] `test_load_model_with_config_retry` — config.toml enables retry
  - [x] `test_load_model_retry_false_overrides_config` — kwarg disables
  - [x] `test_load_model_with_fallback` — wraps with FallbackModule
  - [x] `test_load_model_with_fallback_dict` — custom config dict
  - [x] `test_load_model_retry_and_fallback` — correct stacking order
  - [x] `test_load_model_no_modules` — adapter returned directly (existing behavior)
  - [x] `test_load_model_retry_kwarg_overrides_config_values` — kwarg values override config
- [x] Update `__init__.py` — export BaseModule, RetryModule, FallbackModule (lazy)
- [x] Update `modules/__init__.py` — export module classes

**Verify**: `pytest tests/test_registry.py -v` — all 27 pass including 9 new tests

---

## Phase 5: Full Verification (Task 8)

### T7.8 Full test suite verification `[activity: run-tests]`

- [x] Run `pytest -v` — ALL 194 tests pass
- [x] Run `pytest --cov=arcllm --cov-report=term-missing` — coverage 98%
- [x] Verify module files have >=90% coverage (base: 100%, retry: 97%, fallback: 90%)
- [x] Verify existing tests unaffected — zero regressions
- [x] Count total tests: 194 (149 existing + 45 new)
- [x] Quick smoke test: lazy imports from arcllm work

**Verify**: Full suite green, coverage maintained

---

## Acceptance Criteria

- [x] `BaseModule` wraps any `LLMProvider` with transparent delegation
- [x] `RetryModule` retries on 429/500/502/503/529 + connection errors
- [x] `RetryModule` uses exponential backoff with jitter
- [x] `RetryModule` respects max_retries, raises after exhaustion
- [x] `RetryModule` does NOT retry 400/401/403
- [x] `FallbackModule` walks config chain on failure
- [x] `FallbackModule` creates adapters on-demand via load_model()
- [x] `FallbackModule` raises primary error when chain exhausted
- [x] `load_model("anthropic", retry=True)` enables retry
- [x] `load_model("anthropic", retry=False)` disables even if config enabled
- [x] `load_model("anthropic", retry={"max_retries": 5})` overrides config
- [x] Stacking order: Retry(Fallback(adapter))
- [x] Agent code unchanged — `model.invoke()` just works
- [x] All existing 149 tests pass unchanged
- [x] New module tests are fully mocked
- [x] Zero new dependencies
- [x] Coverage >=97%

---

## Implementation Notes

- ArcLLMAPIError already had `status_code` — no exception changes needed (T7.1)
- Skipped separate pydantic config models for Retry/Fallback — dict-based config is simpler and sufficient
- Circular import between fallback.py and registry.py solved with module-level lazy import wrapper
- Global config cached at module level in registry.py with `_global_config_cache`
- `_resolve_module_config()` handles 4-level priority: kwarg=False > kwarg={dict} > kwarg=True > config.toml enabled
- Module lazy imports added to `__init__.py` for `from arcllm import RetryModule` support
