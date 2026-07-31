# PLAN: Rate Limiter Module

> Implementation tasks for ArcLLM Step 8.
> Status: COMPLETE

---

## Progress

**Completed**: 7/7 tasks
**Remaining**: 0 tasks

---

## Phase 1: Config Update (Task 1)

### T8.1 Update config.toml with burst_capacity `[activity: configuration]`

Add `burst_capacity` to `[modules.rate_limit]` section.

- [x] Edit `src/arcllm/config.toml` — add `burst_capacity = 60` after `requests_per_minute`
- [x] Verify `ModuleConfig` accepts the new field (uses `extra="allow"`)

**Verify**: `pytest -v` — all 219 existing tests still pass

---

## Phase 2: TDD RED — Write Tests First (Task 2)

### T8.2 Write test_rate_limit.py — tests first (TDD RED) `[activity: unit-testing]`

Write comprehensive tests before implementing. All tests should fail with ImportError.

- [x] Create `tests/test_rate_limit.py`
- [x] **TestTokenBucket**:
  - [x] `test_starts_with_full_capacity` — new bucket has `capacity` tokens
  - [x] `test_acquire_consumes_token` — after acquire, tokens decrease by 1
  - [x] `test_acquire_when_empty_waits` — mock asyncio.sleep, verify wait called
  - [x] `test_acquire_returns_wait_time` — returns 0.0 when immediate, >0 when waited
  - [x] `test_refill_adds_tokens_over_time` — mock time.monotonic, advance time, verify tokens added
  - [x] `test_refill_capped_at_capacity` — tokens never exceed capacity
  - [x] `test_burst_allows_multiple_immediate` — capacity=5, 5 acquires return 0.0
  - [x] `test_burst_exhausted_then_waits` — capacity=2, third acquire waits
- [x] **TestRateLimitModule**:
  - [x] `test_invoke_delegates_to_inner` — messages/tools/kwargs passed through
  - [x] `test_invoke_acquires_token` — bucket.acquire() called before inner.invoke()
  - [x] `test_logs_warning_when_throttled` — caplog captures WARNING with provider name and wait time
  - [x] `test_no_log_when_immediate` — no WARNING when acquire returns 0.0
  - [x] `test_provider_name_from_inner` — reads inner.name for bucket lookup
- [x] **TestRateLimitValidation**:
  - [x] `test_zero_rpm_rejected` — ArcLLMConfigError
  - [x] `test_negative_rpm_rejected` — ArcLLMConfigError
  - [x] `test_zero_burst_rejected` — ArcLLMConfigError
  - [x] `test_burst_defaults_to_rpm` — burst_capacity not in config, defaults to RPM value
- [x] **TestBucketRegistry**:
  - [x] `test_same_provider_shares_bucket` — two modules, same provider name, same bucket
  - [x] `test_different_providers_different_buckets` — different names, different buckets
  - [x] `test_clear_buckets_removes_all` — clear, then next load creates fresh bucket
- [x] Run tests — all FAIL (RED confirmed)

**Verify**: Tests written, all fail with ImportError (correct RED)

---

## Phase 3: Implementation (Tasks 3-4)

### T8.3 Implement TokenBucket class `[activity: backend-development]`

- [x] Create `src/arcllm/modules/rate_limit.py`
- [x] Implement `TokenBucket`:
  - [x] `__init__(self, capacity: int, refill_rate: float)` — set capacity, tokens=capacity, last_refill=monotonic(), lock=Lock()
  - [x] `async acquire(self) -> float` — refill, check token, sleep if empty (outside lock), return wait
  - [x] `_refill(self) -> None` — add elapsed * refill_rate, cap at capacity, update timestamp
- [x] Implement module-level registry:
  - [x] `_bucket_registry: dict[str, TokenBucket] = {}`
  - [x] `_get_or_create_bucket(provider, capacity, refill_rate) -> TokenBucket`
  - [x] `clear_buckets() -> None`
- [x] Run TokenBucket tests — GREEN

**Verify**: `pytest tests/test_rate_limit.py::TestTokenBucket -v` — all pass

### T8.4 Implement RateLimitModule `[activity: backend-development]`

- [x] Add `RateLimitModule(BaseModule)` to `rate_limit.py`:
  - [x] `__init__(self, config: dict, inner: LLMProvider)` — validate config, get/create bucket
  - [x] `invoke()` — acquire token, log WARNING if waited, delegate to inner
  - [x] Config parsing: `requests_per_minute`, `burst_capacity` (default=RPM)
  - [x] Validation: RPM > 0, burst >= 1
- [x] Run all rate limit tests — GREEN

**Verify**: `pytest tests/test_rate_limit.py -v` — all pass

---

## Phase 4: Registry Integration (Task 5)

### T8.5 Update registry.py and exports `[activity: backend-development]`

- [x] Update `load_model()` signature — add `rate_limit: bool | dict | None = None`
- [x] Add rate_limit stacking (innermost, before fallback and retry):
  - [x] Move rate_limit wrapping BEFORE fallback wrapping
  - [x] Lazy import: `from arcllm.modules.rate_limit import RateLimitModule`
- [x] Update `clear_cache()` — import and call `clear_buckets()`
- [x] Update `modules/__init__.py` — add `RateLimitModule` export
- [x] Update `__init__.py` — add `RateLimitModule` to lazy imports
- [x] Add registry integration tests to `test_registry.py`:
  - [x] `test_load_model_with_rate_limit` — wraps with RateLimitModule
  - [x] `test_load_model_with_rate_limit_dict` — custom config
  - [x] `test_load_model_rate_limit_false_overrides_config` — kwarg disables
  - [x] `test_load_model_full_stack_order` — Retry(Fallback(RateLimit(adapter)))
  - [x] `test_clear_cache_clears_buckets` — clear_cache removes rate limit state

**Verify**: `pytest tests/test_registry.py -v` — all pass

---

## Phase 5: Full Verification (Tasks 6-7)

### T8.6 Update config.toml `[activity: configuration]`

- [x] Add `burst_capacity = 60` to `[modules.rate_limit]` in config.toml

**Verify**: Config loads without errors

### T8.7 Full test suite verification `[activity: run-tests]`

- [x] Run `pytest -v` — ALL tests pass (219 existing + new)
- [x] Verify existing tests unaffected — zero regressions
- [x] Count total tests: 244 passed, 1 skipped
- [x] Quick smoke test: `from arcllm import RateLimitModule` works

**Verify**: Full suite green

---

## Acceptance Criteria

- [x] `TokenBucket` implements correct token bucket algorithm
- [x] `TokenBucket.acquire()` returns 0.0 when tokens available, >0.0 after waiting
- [x] `TokenBucket` refill capped at capacity
- [x] `TokenBucket` uses `asyncio.Lock` for concurrent safety
- [x] `RateLimitModule` acquires token before each `invoke()` call
- [x] WARNING log emitted when caller waits, with provider name and wait duration
- [x] No log when token immediately available
- [x] Per-provider shared buckets via module-level registry
- [x] `clear_buckets()` resets shared state
- [x] `registry.clear_cache()` also clears buckets
- [x] `load_model("x", rate_limit=True)` enables rate limiting
- [x] `load_model("x", rate_limit=False)` disables even if config enabled
- [x] `load_model("x", rate_limit={"requests_per_minute": 120})` overrides config
- [x] Stacking order: Retry(Fallback(RateLimit(adapter)))
- [x] Config validation: RPM > 0, burst >= 1
- [x] `burst_capacity` defaults to `requests_per_minute`
- [x] Agent code unchanged — `model.invoke()` just works
- [x] All existing 219 tests pass unchanged
- [x] New tests fully mocked (no real API calls, no real time waits)
- [x] Zero new dependencies
