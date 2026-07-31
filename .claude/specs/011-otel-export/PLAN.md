# PLAN: OpenTelemetry Export

> Implementation tasks for ArcLLM Step 13.
> Status: COMPLETE

---

## Progress

**Completed**: 10/10 tasks
**Remaining**: 0 tasks

---

## Phase 1: Dependencies and Config (Tasks 1-2)

### T13.1 Add opentelemetry-api to core deps and otel extras `[activity: configuration]`

Update `pyproject.toml` with new dependencies.

- [x] Add `"opentelemetry-api>=1.20"` to `[project.dependencies]`
- [x] Add `[project.optional-dependencies.otel]` section with SDK + OTLP exporters
- [x] Run `pip install -e ".[dev]"` — verify `from opentelemetry import trace` works
- [x] Verify existing tests still pass (no import conflicts)

**Verify**: `pytest -v` — all 323 existing tests pass, `from opentelemetry import trace` succeeds

---

### T13.2 Add [modules.otel] section to config.toml `[activity: configuration]`

Add the full OTel config section with all enterprise knobs.

- [x] Add `[modules.otel]` section to `src/arcllm/config.toml` with all config keys:
  - `enabled`, `exporter`, `endpoint`, `protocol`, `service_name`, `sample_rate`
  - `headers`, `insecure`, `certificate_file`, `client_key_file`, `client_cert_file`
  - `timeout_ms`, `max_batch_size`, `max_queue_size`, `schedule_delay_ms`
- [x] Add `[modules.otel.resource_attributes]` empty table
- [x] Verify `ModuleConfig` accepts the new fields (uses `extra="allow"`)

**Verify**: `pytest -v` — all existing tests pass

---

## Phase 2: BaseModule Span Support (Task 3)

### T13.3 Add _tracer and _span() to BaseModule `[activity: backend-development]`

TDD: Write tests first, then implement.

- [x] Create `tests/test_base_module.py` with BaseModule span tests:
  - [x] `test_tracer_returns_tracer` — _tracer returns an OTel Tracer instance
  - [x] `test_span_creates_named_span` — _span("name") creates span with that name
  - [x] `test_span_yields_span_object` — context manager yields the span
  - [x] `test_span_records_exception_on_error` — unhandled exception recorded on span
  - [x] `test_span_sets_error_status_on_error` — StatusCode.ERROR set on span
  - [x] `test_span_reraises_exception` — exception propagates to caller
  - [x] `test_span_accepts_attributes` — attributes dict passed to span
  - [x] `test_span_noop_without_sdk` — no crash when tracer is no-op
  - [x] `test_nested_spans_parent_child` — inner _span() auto-parents under outer
  - [x] `test_invoke_unchanged` — existing BaseModule.invoke() behavior unaffected
- [x] Run tests — all FAIL (RED confirmed)
- [x] Implement in `src/arcllm/modules/base.py`:
  - [x] Import `from opentelemetry import trace` and `from opentelemetry.trace import StatusCode`
  - [x] Add `_tracer` property returning `trace.get_tracer("arcllm")`
  - [x] Add `_span(name, attributes=None)` as `contextlib.contextmanager`
- [x] Run tests — all PASS (GREEN)

**Verify**: `pytest tests/test_base_module.py -v` — all pass. `pytest -v` — all 323+ tests pass.

---

## Phase 3: OtelModule (Tasks 4-5)

### T13.4 Write test_otel.py — tests first (TDD RED) `[activity: unit-testing]`

Comprehensive OtelModule tests before implementation.

- [x] Create `tests/test_otel.py`
- [x] **TestOtelModule** (~12 tests):
  - [x] `test_invoke_delegates_to_inner` — messages passed, response returned
  - [x] `test_invoke_passes_tools_and_kwargs` — tools, max_tokens forwarded
  - [x] `test_returns_response_unchanged` — same object reference
  - [x] `test_creates_root_span` — span named "arcllm.invoke" created
  - [x] `test_sets_gen_ai_system_attribute` — gen_ai.system = inner.name
  - [x] `test_sets_gen_ai_request_model` — gen_ai.request.model = inner.model_name
  - [x] `test_sets_gen_ai_usage_input_tokens` — from response.usage
  - [x] `test_sets_gen_ai_usage_output_tokens` — from response.usage
  - [x] `test_sets_gen_ai_response_model` — from response.model
  - [x] `test_sets_gen_ai_response_finish_reasons` — from response.stop_reason
  - [x] `test_provider_name_from_inner` — module.name == inner.name
  - [x] `test_model_name_from_inner` — module.model_name == inner.model_name
- [x] **TestOtelModuleValidation** (~6 tests):
  - [x] `test_invalid_exporter_rejected` — ArcLLMConfigError
  - [x] `test_invalid_protocol_rejected` — ArcLLMConfigError
  - [x] `test_sample_rate_below_zero_rejected` — ArcLLMConfigError
  - [x] `test_sample_rate_above_one_rejected` — ArcLLMConfigError
  - [x] `test_unknown_config_keys_rejected` — ArcLLMConfigError
  - [x] `test_sdk_not_installed_raises_error` — ArcLLMConfigError with install message
- [x] **TestOtelSdkSetup** (~7 tests):
  - [x] `test_otlp_exporter_created` — OTLP exporter with endpoint/protocol
  - [x] `test_console_exporter_created` — ConsoleSpanExporter used
  - [x] `test_none_exporter_no_processor` — no exporter added
  - [x] `test_auth_headers_passed` — headers dict forwarded to exporter
  - [x] `test_resource_includes_service_name` — service.name in Resource
  - [x] `test_resource_includes_custom_attributes` — resource_attributes merged
  - [x] `test_sampler_uses_sample_rate` — TraceIdRatioBased with correct rate
- [x] Run tests — all FAIL (RED confirmed: ImportError)

**Verify**: Tests written, all fail with ImportError (correct RED)

---

### T13.5 Implement OtelModule in modules/otel.py `[activity: backend-development]`

- [x] Create `src/arcllm/modules/otel.py`
- [x] Implement `OtelModule(BaseModule)`:
  - [x] `__init__(config, inner)` — validate config keys, setup SDK if exporter != "none"
  - [x] `_setup_sdk(config)` — configure TracerProvider, exporter, sampler, processor
  - [x] `invoke(messages, tools, **kwargs)` — create root span, set GenAI attributes, delegate
- [x] Valid config keys set: `enabled`, `exporter`, `endpoint`, `protocol`, `service_name`, `sample_rate`, `headers`, `insecure`, `certificate_file`, `client_key_file`, `client_cert_file`, `timeout_ms`, `max_batch_size`, `max_queue_size`, `schedule_delay_ms`, `resource_attributes`
- [x] SDK import guard: `try: from opentelemetry.sdk...` with clear error
- [x] Run all OTel tests — GREEN

**Verify**: `pytest tests/test_otel.py -v` — all pass

---

## Phase 4: Module Span Integration (Tasks 6-7)

### T13.6 Write test_otel_integration.py — module span tests (TDD RED) `[activity: unit-testing]`

Tests for span creation in existing modules.

- [x] Create `tests/test_otel_integration.py`
- [x] **TestRetrySpans** (~5 tests):
  - [x] `test_retry_creates_retry_span` — arcllm.retry span exists
  - [x] `test_retry_creates_attempt_spans` — arcllm.retry.attempt per attempt
  - [x] `test_retry_records_exception_on_failed_attempt` — exception event on span
  - [x] `test_retry_attempt_ok_when_handled` — StatusCode.OK on retried attempt
  - [x] `test_retry_error_on_exhaustion` — StatusCode.ERROR when all retries fail
- [x] **TestFallbackSpans** (~3 tests):
  - [x] `test_fallback_creates_fallback_span` — arcllm.fallback span exists
  - [x] `test_fallback_creates_provider_spans` — per-provider child spans
  - [x] `test_fallback_primary_failed_event` — event recorded when primary fails
- [x] **TestRateLimitSpans** (~3 tests):
  - [x] `test_rate_limit_creates_span` — arcllm.rate_limit span exists
  - [x] `test_rate_limit_records_wait_ms` — arcllm.rate_limit.wait_ms attribute
  - [x] `test_rate_limit_throttled_event` — event when wait > 0
- [x] **TestTelemetrySpans** (~2 tests):
  - [x] `test_telemetry_creates_span` — arcllm.telemetry span exists
  - [x] `test_telemetry_records_duration_and_cost` — attributes on span
- [x] **TestAuditSpans** (~2 tests):
  - [x] `test_audit_creates_span` — arcllm.audit span exists
  - [x] `test_audit_records_metadata` — message_count, content_length attributes
- [x] Run tests — all FAIL (RED)

**Verify**: Tests written, all fail (correct RED)

---

### T13.7 Add span creation to all existing modules `[activity: backend-development]`

Modify each module's `invoke()` to wrap in `self._span()`.

- [x] Update `modules/retry.py`:
  - [x] Wrap retry loop in `self._span("arcllm.retry")`
  - [x] Wrap each attempt in `self._span("arcllm.retry.attempt")`
  - [x] Record exception on attempt span, set OK status (handled)
  - [x] Set ERROR on retry span when all retries exhausted
- [x] Update `modules/fallback.py`:
  - [x] Wrap fallback logic in `self._span("arcllm.fallback")`
  - [x] Wrap each fallback attempt in `self._span("arcllm.fallback.attempt")`
  - [x] Add event when primary fails
- [x] Update `modules/rate_limit.py`:
  - [x] Wrap invoke in `self._span("arcllm.rate_limit")`
  - [x] Set `arcllm.rate_limit.wait_ms` attribute
  - [x] Add event when throttled
- [x] Update `modules/telemetry.py`:
  - [x] Wrap invoke in `self._span("arcllm.telemetry")`
  - [x] Set `arcllm.telemetry.duration_ms` and `arcllm.telemetry.cost_usd` attributes
- [x] Update `modules/audit.py`:
  - [x] Wrap invoke in `self._span("arcllm.audit")`
  - [x] Set `arcllm.audit.message_count`, `arcllm.audit.content_length` attributes
  - [x] Set conditional `arcllm.audit.tools_provided`, `arcllm.audit.tool_calls`
- [x] Run integration tests — GREEN
- [x] Run ALL existing tests — verify zero regressions

**Verify**: `pytest tests/test_otel_integration.py -v` — all pass. `pytest -v` — all tests pass.

---

## Phase 5: Registry Integration (Task 8)

### T13.8 Update registry.py with otel= kwarg `[activity: backend-development]`

- [x] Update `load_model()` signature — add `otel: bool | dict[str, Any] | None = None`
- [x] Add OTel stacking (outermost, after telemetry):
  - [x] Resolve otel config via `_resolve_module_config("otel", otel)`
  - [x] Wrap: `result = OtelModule(otel_config, result)`
- [x] Update docstring: stacking order `Otel -> Telemetry -> Audit -> Retry -> Fallback -> RateLimit -> Adapter`
- [x] Add registry integration tests to `test_registry.py`:
  - [x] `test_load_model_with_otel` — wraps with OtelModule
  - [x] `test_load_model_otel_full_stack` — Otel(Telemetry(Audit(Retry(Fallback(RateLimit(adapter))))))
  - [x] `test_load_model_otel_false_overrides_config` — kwarg disables config
  - [x] `test_load_model_otel_dict_overrides_config` — kwarg dict merges
  - [x] `test_load_model_otel_only` — OTel without other modules

**Verify**: `pytest tests/test_registry.py -v` — all pass

---

## Phase 6: Export Updates (Task 9)

### T13.9 Update __init__.py and modules/__init__.py exports `[activity: backend-development]`

- [x] Update `src/arcllm/modules/__init__.py` — add `OtelModule` import and `__all__`
- [x] Update `src/arcllm/__init__.py` — add `OtelModule` to `_LAZY_IMPORTS` and `__all__`

**Verify**: `from arcllm import OtelModule` works

---

## Phase 7: Full Verification (Task 10)

### T13.10 Full test suite verification `[activity: run-tests]`

- [x] Run `pytest -v` — ALL tests pass (323 existing + 56 new = 379 total)
- [x] Verify existing tests unaffected — zero regressions
- [x] Count total tests — 379 passed, 1 skipped
- [x] Quick smoke test: `from arcllm import OtelModule` works
- [x] Verify `from opentelemetry import trace` works (core dep)
- [x] Verify OTel tests pass WITHOUT opentelemetry-sdk installed (mocked/no-op)
- [x] Update state file and decision log

**Verify**: Full suite green

---

## Acceptance Criteria

- [ ] `opentelemetry-api>=1.20` added to core dependencies
- [ ] `opentelemetry-sdk` + OTLP exporters as `[otel]` optional extras
- [ ] BaseModule has `_tracer` property and `_span()` context manager
- [ ] `_span()` records exceptions and sets ERROR on unhandled errors
- [ ] `_span()` re-raises exceptions (transparent to existing error handling)
- [ ] `_span()` is no-op when no SDK configured
- [ ] `OtelModule` creates `arcllm.invoke` root span
- [ ] Root span has GenAI semantic convention attributes (gen_ai.system, gen_ai.request.model, gen_ai.usage.*, gen_ai.response.*)
- [ ] Root span auto-nests under parent context (agent framework)
- [ ] RetryModule creates `arcllm.retry` and `arcllm.retry.attempt` spans
- [ ] RetryModule records exceptions on attempt spans with OK status (handled)
- [ ] RetryModule sets ERROR only when all retries exhausted
- [ ] FallbackModule creates `arcllm.fallback` and per-provider spans
- [ ] RateLimitModule creates `arcllm.rate_limit` span with wait_ms attribute
- [ ] TelemetryModule creates `arcllm.telemetry` span with duration and cost
- [ ] AuditModule creates `arcllm.audit` span with metadata attributes
- [ ] Config-driven SDK setup: exporter, endpoint, protocol, sample_rate
- [ ] Auth headers, TLS (mTLS), batch tuning, resource attributes configurable
- [ ] Invalid config rejected with ArcLLMConfigError
- [ ] SDK not installed + enabled = clear error with install instructions
- [ ] `load_model("x", otel=True)` enables OTel
- [ ] `load_model("x", otel=False)` disables even if config enabled
- [ ] `load_model("x", otel={"exporter": "console"})` overrides config
- [ ] Stacking: Otel(Telemetry(Audit(Retry(Fallback(RateLimit(adapter))))))
- [ ] All existing 323 tests pass unchanged (zero regressions)
- [ ] New tests fully mocked (no real SDK, no real exporters)
- [ ] OTel API no-op when SDK not configured (zero overhead)
