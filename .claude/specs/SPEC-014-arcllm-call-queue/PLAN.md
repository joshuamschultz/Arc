# PLAN: ArcLLM Call Queue

**SPEC-014** | **Status**: COMPLETE | **Phases**: 3

## Phase 1: Exceptions + Module Implementation (TDD)

> Core implementation with failing tests first.

### Task 1.1: Add Queue Exceptions
- [x] Add `QueueFullError` and `QueueTimeoutError` to `packages/arcllm/src/arcllm/exceptions.py`
- **Reqs**: R-005
- **Files**: `exceptions.py`

### Task 1.2: Write Failing Tests
- [x] Create `packages/arcllm/tests/test_queue.py` with 6 test cases:
  1. `test_invoke_succeeds_within_concurrency_limit` — 2 concurrent calls with max_concurrent=2 both succeed
  2. `test_concurrency_limits_enforced` — 3rd call waits when max_concurrent=2, proceeds after first completes
  3. `test_backpressure_rejects_when_full` — raises `QueueFullError` when `max_queued` exceeded
  4. `test_send_time_timeout_raises_error` — raises `QueueTimeoutError` when inner.invoke() exceeds call_timeout
  5. `test_queue_wait_excluded_from_timeout` — long queue wait + fast inner call succeeds (timeout only covers inner call)
  6. `test_otel_span_attributes_set` — verifies `arc.queue.wait_ms`, `arc.queue.depth`, `arc.queue.call_timeout_ms` set on span
- **Reqs**: R-001, R-002, R-003, R-004, R-005, D-040
- **Files**: `tests/test_queue.py`
- **Verify**: All 6 tests fail with ImportError or missing class ✓

### Task 1.3: Implement QueueModule
- [x] Create `packages/arcllm/src/arcllm/modules/queue.py`
  - Extend `BaseModule`
  - `__init__`: validate config keys, create `BoundedSemaphore`, set defaults
  - `invoke()`: backpressure check → wait for semaphore → set span attributes → `asyncio.wait_for(inner.invoke(), timeout)` → catch `TimeoutError` → raise `QueueTimeoutError`
- **Reqs**: R-001, R-002, R-003, R-004, R-009
- **Files**: `modules/queue.py`
- **Verify**: All 6 tests pass ✓

### Task 1.4: Config Validation Test
- [x] Add test for invalid config keys (e.g., `QueueModule({"bad_key": 1}, mock_inner)` raises `ArcLLMConfigError`)
- [x] Add test for invalid config values (e.g., `max_concurrent=0` raises `ArcLLMConfigError`)
- **Files**: `tests/test_queue.py`
- **Verify**: Tests pass ✓

**Phase 1 Completion**: 12 tests pass (6 core + 1 otel rejection + 5 config validation), `queue.py` + exceptions implemented ✓

---

## Phase 2: Registry + Config Integration

> Wire QueueModule into the arcllm loading pipeline.

### Task 2.1: Add Config Defaults
- [x] Add `[modules.queue]` section to `packages/arcllm/src/arcllm/config.toml`
- **Reqs**: R-006
- **Files**: `config.toml`

### Task 2.2: Update Registry
- [x] Add `queue: bool | dict[str, Any] | None = None` parameter to `load_model()` in `registry.py`
- [x] Add queue module wrapping between telemetry and otel blocks
- [x] Update `load_model()` docstring: add `queue` kwarg docs, update stack order
- **Reqs**: R-006, R-007
- **Files**: `registry.py`

### Task 2.3: Update Module Package
- [x] Add `QueueModule` import to `modules/__init__.py`
- [x] Add `"QueueModule"` to `__all__`
- [x] Add `QueueModule`, `QueueFullError`, `QueueTimeoutError` to `__init__.py` lazy imports and `__all__`
- **Files**: `modules/__init__.py`, `__init__.py`

### Task 2.4: Registry Integration Test
- [x] Add test: `load_model("anthropic", queue=True)` includes QueueModule in wrapping stack
- [x] Add test: `load_model("anthropic", queue=False)` does NOT include QueueModule
- [x] Add test: `load_model("anthropic", queue={"max_concurrent": 5})` uses override config
- [x] Update full-stack test to include QueueModule in correct position
- **Verify**: All 49 registry tests pass ✓

**Phase 2 Completion**: QueueModule loadable via `load_model()`, all tests pass ✓

---

## Phase 3: Verification + Cleanup

> Quality gates and final verification.

### Task 3.1: Ruff + Mypy
- [x] Run `ruff check` — 0 errors
- [x] Run `ruff format` — all files formatted
- [x] Run `mypy --strict` on new files — 0 errors (pre-existing errors in other files unchanged)
- **Verify**: All quality gates pass ✓

### Task 3.2: Full Test Suite
- [x] Run `pytest tests/ -v` — 725 passed, 2 failed (pre-existing: azure_openai config, otel retry)
- [x] Verify no regressions: 0 new failures
- **Verify**: 0 new failures ✓

### Task 3.3: Update clear_cache
- [x] Verified: `clear_cache()` needs no queue-specific cleanup (per-instance state only, no module-level caches)

**Phase 3 Completion**: All quality gates pass, SPEC-014 status → COMPLETE ✓

---

## Summary

| Phase | Tasks | Tests | Files Modified | Files Created |
|-------|-------|-------|----------------|---------------|
| 1 | 4 | 12 | 1 (exceptions.py) | 2 (queue.py, test_queue.py) |
| 2 | 4 | 4 | 4 (registry.py, config.toml, modules/__init__.py, __init__.py) | 0 |
| 3 | 3 | 0 | 0 | 0 |
| **Total** | **11** | **16** | **5** | **2** |

## Completion Checklist

- [x] All tests pass (unit) — 12 queue + 4 registry integration
- [x] Types check (mypy --strict) — 0 errors on new files
- [x] Linter clean (ruff check) — 0 errors
- [x] Audit events via structured logging on all queue operations (enqueue, dequeue, timeout, reject)
- [x] Docstrings on QueueModule, QueueFullError, QueueTimeoutError
- [x] No new dependencies added (stdlib asyncio only)
- [x] Stack order documented in load_model() docstring
- [x] README.md learnings updated
