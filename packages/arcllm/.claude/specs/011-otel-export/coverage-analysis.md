# Coverage Analysis: Spec 011 - OpenTelemetry Export

**Date**: 2026-02-11
**Test Suite**: 538 passed, 1 skipped (full suite)
**Overall Project Coverage**: 94% line, 95% branch (full suite)

---

## 1. Coverage Summary by File

### OTel-Specific Files (from OTel-focused test run)

| File | Stmts | Miss | Branch | BrPart | Line % | Branch % |
|------|-------|------|--------|--------|--------|----------|
| `modules/otel.py` | 78 | 35 | 18 | 0 | **57%** | **0%** |
| `modules/base.py` | 35 | 0 | 2 | 0 | **100%** | **100%** |

### OTel-Related Files (full suite context)

| File | Line % | Missing Lines |
|------|--------|---------------|
| `modules/otel.py` | **57%** | 42-44, 50-119 |
| `modules/base.py` | **100%** | None |
| `registry.py` | **86%** | 83-86, 181-193, 222-224 |

### Verdict

- `base.py` -- FULLY COVERED. No gaps.
- `otel.py` -- CRITICAL GAP. The entire `_setup_sdk()` function (lines 40-119) is untested through real execution. All 25 `test_otel.py` SDK tests mock `_setup_sdk()` at the boundary -- they verify the config is passed but never exercise the actual SDK wiring.
- `registry.py` -- 86% is acceptable; the missing lines are unrelated to OTel (Ollama adapter loading path, etc.).

---

## 2. Detailed Gap Analysis: `otel.py`

### Lines 42-44: SDK import error path (inside `_setup_sdk`)
```python
except ImportError:
    raise ArcLLMConfigError(
        "OTel SDK not installed. Run: pip install arcllm[otel]"
    )
```
**Status**: NEVER EXECUTED. The test `test_sdk_not_installed_raises_error` patches `sys.modules` at the top level to block the import in `OtelModule.__init__`, but `_setup_sdk()` is mocked out in `TestOtelSdkSetup`. The actual `try/except ImportError` in `_setup_sdk` at line 40-48 is never hit.

**Priority**: P1 (High) -- This is the user-facing error message when the SDK optional dependency is missing. If it breaks, users get a confusing traceback instead of a clean install instruction.

### Lines 50-119: The entire `_setup_sdk()` function body
```
50: from opentelemetry import trace
52-55: Resource creation
57-59: Sampler creation
61-62: TracerProvider creation
64-108: Exporter creation (OTLP gRPC, OTLP HTTP, Console branches)
111-118: BatchSpanProcessor creation and registration
119: trace.set_tracer_provider(provider)
```
**Status**: ZERO LINE COVERAGE. Tests in `TestOtelSdkSetup` all mock `_setup_sdk` itself, so they prove config is forwarded but never prove the function works.

**Sub-gaps by criticality**:

| Lines | What | Priority | Impact |
|-------|------|----------|--------|
| 50-62 | Resource + Sampler + TracerProvider init | P0 (Critical) | Core SDK wiring; if broken, no traces export at all |
| 66-88 | OTLP gRPC exporter branch | P0 (Critical) | Default export path for production; most common config |
| 89-103 | OTLP HTTP exporter branch | P1 (High) | Alternative export protocol; used when gRPC blocked by firewall |
| 74-82 | gRPC ImportError handler | P1 (High) | Error path when grpc extra not installed |
| 90-98 | HTTP ImportError handler | P1 (High) | Error path when http extra not installed |
| 104-107 | Console exporter branch | P2 (Medium) | Dev/debug only; low production risk |
| 108-109 | Unreachable else branch | P3 (Low) | Dead code after validation; defensive |
| 111-119 | BatchSpanProcessor + set_tracer_provider | P0 (Critical) | Final wiring step; broken = silent no-op |

---

## 3. Branch Coverage Analysis

### `otel.py` Branch Coverage: 0 of 18 branches covered

The 18 branches map to:

1. `if unknown:` (config key validation) -- covered by `test_unknown_config_keys_rejected` but only the truthy branch
2. `if exporter not in _VALID_EXPORTERS` -- covered truthy branch only
3. `if protocol not in _VALID_PROTOCOLS` -- covered truthy branch only
4. `if sample_rate < 0.0` -- covered truthy branch only
5. `if sample_rate > 1.0` -- covered truthy branch only
6. `if exporter != "none"` -- both branches exercised (none tests skip, otlp/console tests call mock)
7-18. All branches inside `_setup_sdk()` -- 0% (function body never executed)

Key missing branches in `_setup_sdk`:
- `if exporter_type == "otlp"` vs `elif exporter_type == "console"` vs `else`
- `if protocol == "grpc"` vs `else` (http)
- `try/except ImportError` for gRPC exporter
- `try/except ImportError` for HTTP exporter
- `headers or None` ternary (empty headers vs populated)
- `config.get("insecure", False)` default vs explicit

### `base.py` Branch Coverage: 100%

Both branches (exception path and happy path in `_span`) are fully covered.

---

## 4. Missing Edge Case Tests

### A. Error Paths Not Tested

| # | Test Gap | Risk | Priority |
|---|----------|------|----------|
| 1 | `_setup_sdk` ImportError for SDK core (lines 40-48) executed directly | Users get wrong error | P1 |
| 2 | `_setup_sdk` ImportError for gRPC exporter (lines 74-82) | Users get wrong error | P1 |
| 3 | `_setup_sdk` ImportError for HTTP exporter (lines 90-98) | Users get wrong error | P1 |
| 4 | `OtelModule.invoke()` when inner provider raises exception | Span error status not verified | P1 |
| 5 | `OtelModule.invoke()` when inner returns None/malformed response | Attribute setting crashes | P2 |

### B. Configuration Edge Cases Not Tested

| # | Test Gap | Risk | Priority |
|---|----------|------|----------|
| 6 | `sample_rate=0.0` (boundary -- should be valid) | Off-by-one rejection | P2 |
| 7 | `sample_rate=1.0` (boundary -- should be valid) | Off-by-one rejection | P2 |
| 8 | Empty `headers={}` vs `headers=None` | `headers or None` logic | P2 |
| 9 | `timeout_ms` integer division behavior (e.g., 999 -> 0) | Silent zero timeout | P2 |
| 10 | `max_batch_size`, `max_queue_size`, `schedule_delay_ms` defaults | Batch tuning correctness | P3 |
| 11 | Non-string exporter type (e.g., `exporter=123`) | Type confusion | P3 |

### C. Integration Gaps

| # | Test Gap | Risk | Priority |
|---|----------|------|----------|
| 12 | `_setup_sdk` called with actual SDK (not mocked) for `console` exporter | Real SDK wiring verified | P0 |
| 13 | `_setup_sdk` called with actual SDK for `otlp` exporter (grpc) | Real SDK wiring verified | P0 |
| 14 | Full stack: OTel wrapping retry wrapping fallback, with failure | Nested span tree correctness | P2 |
| 15 | OTel span attributes when response has zero tokens | Edge case attribute values | P3 |

---

## 5. Prioritized Improvement Plan

### Phase 1: Critical (P0) -- Must Fix

**Expected coverage increase**: otel.py 57% -> ~85%

#### Test 1: `_setup_sdk` actually executes with console exporter
```python
def test_setup_sdk_console_exporter_real():
    """_setup_sdk with console exporter wires TracerProvider."""
    from arcllm.modules.otel import _setup_sdk
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    _setup_sdk({"exporter": "console", "sample_rate": 1.0})
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
```
**Effort**: Small (15 min). Covers lines 50-62, 104-107, 111-119.
**Coverage gain**: +20 lines (~25% of otel.py).

#### Test 2: `_setup_sdk` with OTLP gRPC exporter (real SDK, mocked network)
```python
def test_setup_sdk_otlp_grpc_real():
    """_setup_sdk with otlp/grpc wires OTLP exporter into TracerProvider."""
    from arcllm.modules.otel import _setup_sdk
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    _setup_sdk({
        "exporter": "otlp",
        "protocol": "grpc",
        "endpoint": "http://localhost:4317",
        "sample_rate": 1.0,
    })
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
```
**Effort**: Small (15 min). Covers lines 66-88.
**Coverage gain**: +22 lines.

#### Test 3: `_setup_sdk` with OTLP HTTP exporter
```python
def test_setup_sdk_otlp_http_real():
    """_setup_sdk with otlp/http wires HTTP exporter."""
    from arcllm.modules.otel import _setup_sdk
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    _setup_sdk({
        "exporter": "otlp",
        "protocol": "http",
        "endpoint": "http://localhost:4318",
        "sample_rate": 1.0,
    })
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
```
**Effort**: Small (15 min). Covers lines 89-103.
**Coverage gain**: +14 lines.

**Note**: These tests set the global tracer provider. Each test should reset it afterward to avoid test pollution. Consider a fixture:
```python
@pytest.fixture(autouse=True)
def _reset_tracer_provider():
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    original = trace.get_tracer_provider()
    yield
    # Reset to avoid global state leakage
    trace.set_tracer_provider(original)
```

### Phase 2: High Impact (P1) -- Should Fix

**Expected coverage increase**: otel.py ~85% -> ~95%

#### Test 4: ImportError for SDK core packages
```python
def test_setup_sdk_missing_sdk_raises():
    """_setup_sdk raises ArcLLMConfigError when SDK not installed."""
    from unittest.mock import patch
    import importlib

    with patch.dict("sys.modules", {
        "opentelemetry.sdk.resources": None,
        "opentelemetry.sdk.trace": None,
        "opentelemetry.sdk.trace.export": None,
        "opentelemetry.sdk.trace.sampling": None,
    }):
        from arcllm.modules.otel import _setup_sdk
        with pytest.raises(ArcLLMConfigError, match="install"):
            _setup_sdk({"exporter": "otlp"})
```
**Effort**: Medium (20 min). Covers lines 42-48.

#### Test 5: ImportError for gRPC exporter
```python
def test_setup_sdk_missing_grpc_exporter_raises():
    """Missing gRPC exporter package raises clear error."""
    with patch.dict("sys.modules", {
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": None,
    }):
        from arcllm.modules.otel import _setup_sdk
        with pytest.raises(ArcLLMConfigError, match="gRPC"):
            _setup_sdk({"exporter": "otlp", "protocol": "grpc"})
```
**Effort**: Small (15 min). Covers lines 74-82.

#### Test 6: ImportError for HTTP exporter
```python
def test_setup_sdk_missing_http_exporter_raises():
    """Missing HTTP exporter package raises clear error."""
    with patch.dict("sys.modules", {
        "opentelemetry.exporter.otlp.proto.http.trace_exporter": None,
    }):
        from arcllm.modules.otel import _setup_sdk
        with pytest.raises(ArcLLMConfigError, match="HTTP"):
            _setup_sdk({"exporter": "otlp", "protocol": "http"})
```
**Effort**: Small (15 min). Covers lines 90-98.

#### Test 7: `invoke()` when inner provider raises
```python
async def test_invoke_records_error_span_on_inner_exception():
    """Exception from inner provider is recorded on span and re-raised."""
    from arcllm.modules.otel import OtelModule

    inner = _make_inner()
    inner.invoke = AsyncMock(side_effect=RuntimeError("provider down"))
    module = OtelModule({"exporter": "none"}, inner)

    mock_tracer = MagicMock()
    mock_span = MagicMock()
    mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(return_value=mock_span)
    mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)

    with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
        with pytest.raises(RuntimeError, match="provider down"):
            await module.invoke([Message(role="user", content="hi")])

    mock_span.record_exception.assert_called_once()
    mock_span.set_status.assert_called_once_with(StatusCode.ERROR)
```
**Effort**: Small (15 min). Verifies error propagation through span.

### Phase 3: Medium Impact (P2) -- Nice to Have

#### Test 8: Boundary sample rates
```python
def test_sample_rate_zero_accepted():
    """sample_rate=0.0 is valid (sample nothing)."""
    OtelModule({"exporter": "none", "sample_rate": 0.0}, _make_inner())

def test_sample_rate_one_accepted():
    """sample_rate=1.0 is valid (sample everything)."""
    OtelModule({"exporter": "none", "sample_rate": 1.0}, _make_inner())
```

#### Test 9: Headers edge case
```python
def test_setup_sdk_empty_headers_becomes_none():
    """Empty headers dict converts to None for exporter."""
    # Verify headers={} -> headers or None -> None
```

#### Test 10: Timeout integer division
```python
def test_setup_sdk_timeout_ms_division():
    """timeout_ms=999 produces timeout=0 (integer division)."""
    # Document that 999ms -> 0s timeout via // 1000
```

---

## 6. Summary

| Metric | Current | After Phase 1 | After Phase 2 | Target |
|--------|---------|---------------|---------------|--------|
| `otel.py` Line % | **57%** | ~85% | ~95% | >=90% |
| `otel.py` Branch % | **0%** | ~60% | ~85% | >=75% |
| `base.py` Line % | **100%** | 100% | 100% | 100% |
| `base.py` Branch % | **100%** | 100% | 100% | 100% |

### Critical Finding

The entire `_setup_sdk()` function -- 70 lines of SDK wiring code -- has **zero test coverage through real execution**. The existing `TestOtelSdkSetup` class mocks `_setup_sdk` at the call boundary, which verifies config forwarding but not correctness. This means:

1. If the SDK API changes, tests will not catch it.
2. If the exporter constructor signatures change, tests will not catch it.
3. If the `BatchSpanProcessor` parameter names change, tests will not catch it.
4. The three `ImportError` handlers have never been proven to work.

This is the single highest-value gap to close. Phase 1 (3 tests, ~45 min) would raise `otel.py` from 57% to ~85% and cover all three exporter branches plus the core SDK wiring.

### Top 3 Critical Gaps

1. **`_setup_sdk()` never executed with real SDK** -- 70 lines of untested wiring (P0)
2. **ImportError handlers never triggered directly** -- 3 error paths silently untested (P1)
3. **`invoke()` error propagation through OTel span untested** -- exception recording not verified (P1)
