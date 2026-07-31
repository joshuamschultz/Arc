# PLAN: Audit Trail Module

> Implementation tasks for ArcLLM Step 11.
> Status: COMPLETE

---

## Progress

**Completed**: 7/7 tasks
**Remaining**: 0 tasks

---

## Phase 1: Config Verification (Task 1)

### T11.1 Verify config.toml audit section `[activity: configuration]`

Confirm `[modules.audit]` section exists in config.toml with `enabled = false`.

- [x] Verify `src/arcllm/config.toml` has `[modules.audit]` with `enabled = false`
- [x] Verify `ModuleConfig` accepts additional fields via `extra="allow"`

**Verify**: Config loads without errors

---

## Phase 2: TDD RED — Write Tests First (Task 2)

### T11.2 Write test_audit.py — tests first (TDD RED) `[activity: unit-testing]`

Write comprehensive tests before implementing. All tests should fail with ImportError.

- [x] Create `tests/test_audit.py`
- [x] **TestAuditModule** (11 tests):
  - [x] `test_invoke_delegates_to_inner` — messages passed to inner, result returned
  - [x] `test_invoke_passes_tools_and_kwargs` — tools and max_tokens forwarded
  - [x] `test_returns_response_unchanged` — same object reference (is check)
  - [x] `test_logs_basic_audit_fields` — provider, model, message_count, stop_reason in caplog
  - [x] `test_logs_tool_info_when_tools_provided` — tools_provided=1 in log
  - [x] `test_logs_no_tools_field_when_none` — tools_provided NOT in log
  - [x] `test_logs_tool_call_count` — tool_calls=2 when 2 tool calls in response
  - [x] `test_logs_content_length` — content_length=12 for "Hello there!"
  - [x] `test_content_length_zero_when_none` — content_length=0 when content is None
  - [x] `test_no_messages_logged_by_default` — "You are helpful" NOT in log (PII safety)
  - [x] `test_no_response_logged_by_default` — "Hello there!" NOT in log (PII safety)
- [x] **TestAuditContentLogging** (3 tests):
  - [x] `test_include_messages_logs_message_content` — "You are helpful" IN log at DEBUG
  - [x] `test_include_response_logs_response_content` — "Hello there!" IN log at DEBUG
  - [x] `test_include_both` — both message and response content in log
- [x] **TestAuditLogLevel** (3 tests):
  - [x] `test_default_log_level_is_info` — "Audit" in log at INFO
  - [x] `test_custom_log_level` — not visible at INFO, visible at DEBUG
  - [x] `test_invalid_log_level_rejected` — ArcLLMConfigError
- [x] **TestAuditProviderInfo** (2 tests):
  - [x] `test_provider_name_from_inner` — module.name == "my-provider"
  - [x] `test_model_name_from_inner` — module.model_name == "test-model"
- [x] Run tests — all FAIL (RED confirmed: ImportError)

**Verify**: Tests written, all fail with ImportError (correct RED)

---

## Phase 3: Implementation (Task 3)

### T11.3 Implement AuditModule in modules/audit.py `[activity: backend-development]`

- [x] Create `src/arcllm/modules/audit.py`
- [x] Implement `AuditModule(BaseModule)`:
  - [x] `__init__(config, inner)` — extract include_messages, include_response, validate log_level
  - [x] `invoke(messages, tools, **kwargs)` — delegate to inner, build audit log parts, conditional tools/tool_calls fields, log at configured level, optional content logging at DEBUG
- [x] Logger: `logging.getLogger(__name__)` — logger name: `arcllm.modules.audit`
- [x] Valid log levels: `{"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}`
- [x] Uses shared `log_structured()` helper from `_logging.py`
- [x] Strict config key validation via `_VALID_CONFIG_KEYS`
- [x] Run all audit tests — GREEN (19/19)

**Verify**: `pytest tests/test_audit.py -v` — 19/19 passed

---

## Phase 4: Registry Integration (Task 4)

### T11.4 Update registry.py with audit kwarg `[activity: backend-development]`

- [x] Update `load_model()` signature — add `audit: bool | dict | None = None`
- [x] Add audit stacking (between retry and telemetry):
  - [x] Resolve audit config via `_resolve_module_config("audit", audit)`
  - [x] Wrap: `result = AuditModule(audit_config, result)` (after retry, before telemetry)
- [x] Update docstring: stacking order `Telemetry -> Audit -> Retry -> Fallback -> RateLimit -> Adapter`
- [x] Add registry integration tests to `test_registry.py`:
  - [x] `test_load_model_with_audit` — wraps with AuditModule
  - [x] `test_load_model_audit_false_overrides_config` — kwarg disables config
  - [x] `test_load_model_full_stack_with_audit` — Telemetry(Audit(Retry(Fallback(RateLimit(adapter)))))

**Verify**: `pytest tests/test_registry.py -v` — 41/41 passed

---

## Phase 5: Export Updates (Task 5)

### T11.5 Update __init__.py and modules/__init__.py exports `[activity: backend-development]`

- [x] Update `src/arcllm/modules/__init__.py` — add `AuditModule` import and `__all__`
- [x] Update `src/arcllm/__init__.py` — add `AuditModule` to `_LAZY_IMPORTS` and `__all__`

**Verify**: `from arcllm import AuditModule` — works

---

## Phase 6: Full Verification (Task 6)

### T11.6 Full test suite verification `[activity: run-tests]`

- [x] Run `pytest -v` — ALL tests pass (289 existing + 19 new audit + 3 new registry = 311 passed, 1 skipped)
- [x] Verify existing tests unaffected — zero regressions
- [x] Count total tests: 311 passed, 1 skipped
- [x] Quick smoke test: `from arcllm import AuditModule` — works

**Verify**: Full suite green — 311 passed, 1 skipped

---

## Phase 7: State Update (Task 7)

### T11.7 Update state and decision log `[activity: documentation]`

- [x] Update `.claude/arcllm-state.json` — mark Step 11 complete, add D-070 through D-074
- [x] Update `.claude/decision-log.md` — append D-070 through D-074
- [x] Add notes about Step 11 completion

**Verify**: State file reflects current position

---

## Acceptance Criteria

- [x] `AuditModule` wraps invoke() and logs audit metadata after each call
- [x] Log includes provider, model, message_count, stop_reason, content_length
- [x] `tools_provided` logged only when tools arg is not None
- [x] `tool_calls` logged only when response has tool_calls
- [x] Content length calculated as `len(response.content or "")`
- [x] Raw message content NOT logged by default (PII safety)
- [x] Raw response content NOT logged by default (PII safety)
- [x] `include_messages=True` logs message content at DEBUG level
- [x] `include_response=True` logs response content at DEBUG level
- [x] Configurable log level (default INFO)
- [x] Invalid log_level raises `ArcLLMConfigError`
- [x] `load_model("x", audit=True)` enables audit
- [x] `load_model("x", audit=False)` disables even if config enabled
- [x] `load_model("x", audit={"include_messages": True})` overrides config
- [x] Stacking order: Telemetry(Audit(Retry(Fallback(RateLimit(adapter)))))
- [x] Agent code unchanged — `model.invoke()` just works
- [x] All existing 289 tests pass unchanged
- [x] 19 new audit tests + 3 new registry tests — all passing
- [x] New tests fully mocked (no real API calls)
- [x] Zero new dependencies
- [x] Uses shared `log_structured()` helper (consistent with TelemetryModule)
- [x] Strict config key validation (catches typos at construction)
- [x] Total: 311 passed, 1 skipped
