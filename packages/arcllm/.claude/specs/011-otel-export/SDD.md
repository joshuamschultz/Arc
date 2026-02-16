# SDD: OpenTelemetry Export

> System design for ArcLLM Step 13.
> References steering docs in `.claude/steering/`.

---

## Design Overview

Step 13 adds OpenTelemetry distributed tracing to ArcLLM through two complementary changes: (1) deep integration in BaseModule that provides `_tracer` and `_span()` helper so every module creates child spans, and (2) a new OtelModule that creates the root span and handles config-driven SDK setup.

The architecture follows OTel's library instrumentation pattern: `opentelemetry-api` is a core dependency (no-op when SDK not configured), while `opentelemetry-sdk` and exporters are optional extras. This means ArcLLM always creates spans (via the API), but they only get exported when the consumer installs the SDK and configures an exporter.

Key design insight: OtelModule creates a root span that automatically becomes a child span when an agent framework provides parent context. This gives standalone users traces out of the box while seamlessly integrating with the future BlackArc agent framework.

Design priorities:
1. **Zero overhead when disabled** — no-op tracer/spans when SDK not configured
2. **Full waterfall visibility** — every module creates child spans showing retry attempts, fallback hops, rate-limit waits
3. **GenAI semantic conventions** — vendor dashboards (Datadog, Grafana) auto-detect and visualize
4. **Enterprise config** — mTLS, auth headers, batch tuning, sampling via TOML
5. **Clean error recording** — exceptions recorded as events, ERROR status only on true failures

---

## Directory Map

```
src/arcllm/
├── modules/
│   ├── __init__.py                    # MODIFY: Add OtelModule export
│   ├── _logging.py                    # UNCHANGED
│   ├── base.py                        # MODIFY: Add _tracer property, _span() helper
│   ├── otel.py                        # NEW: OtelModule + SDK setup
│   ├── retry.py                       # MODIFY: Add span creation in invoke()
│   ├── fallback.py                    # MODIFY: Add span creation in invoke()
│   ├── rate_limit.py                  # MODIFY: Add span creation in invoke()
│   ├── telemetry.py                   # MODIFY: Add span creation in invoke()
│   └── audit.py                       # MODIFY: Add span creation in invoke()
├── registry.py                        # MODIFY: Add otel= kwarg, outermost stacking
├── __init__.py                        # MODIFY: Add OtelModule to lazy imports
├── config.toml                        # MODIFY: Add [modules.otel] section
├── pyproject.toml                     # MODIFY: Add opentelemetry-api dep + otel extras
tests/
├── test_otel.py                       # NEW: OtelModule + SDK setup tests
├── test_base_module.py                # NEW: BaseModule _tracer/_span() tests
├── test_otel_integration.py           # NEW: Module span creation integration tests
├── test_registry.py                   # MODIFY: Add otel stacking tests
```

---

## Component Design

### 1. BaseModule Changes (`modules/base.py`)

Add `_tracer` property and `_span()` context manager so all modules can create child spans.

| Addition | Type | Purpose |
|----------|------|---------|
| `_tracer` | `property -> Tracer` | Returns OTel Tracer via `trace.get_tracer("arcllm", version)` |
| `_span(name, attributes)` | `context manager` | Creates a span, yields it, handles exception recording |

#### _tracer Property

```
@property
def _tracer(self):
    return trace.get_tracer("arcllm", arcllm.__version__)
```

Key: `trace.get_tracer()` returns a no-op tracer when no SDK is configured. Zero cost.

#### _span() Context Manager

```
@contextmanager
def _span(self, name, attributes=None):
    with self._tracer.start_as_current_span(name, attributes=attributes) as span:
        try:
            yield span
        except Exception as e:
            span.record_exception(e)
            span.set_status(StatusCode.ERROR, str(e))
            raise
```

Design notes:
- Uses `start_as_current_span` — auto-parents under the current active span
- Records exception AND sets ERROR status on unhandled errors
- Re-raises the exception (transparent to existing error handling)
- Subclasses can override error behavior (e.g., RetryModule marks attempt spans OK when retrying)

### 2. OtelModule (`modules/otel.py`)

Root span creator + config-driven SDK setup.

| Attribute | Type | Purpose |
|-----------|------|---------|
| `_exporter_type` | `str` | "otlp", "console", or "none" |
| `_service_name` | `str` | OTel service.name resource attribute |
| `_configured` | `bool` | Whether SDK was successfully configured |

| Method | Purpose |
|--------|---------|
| `__init__(config, inner)` | Validate config, setup SDK if needed |
| `_setup_sdk(config)` | Configure TracerProvider + exporter + sampler |
| `async invoke(messages, tools, **kwargs) -> LLMResponse` | Create root span, set attributes, delegate |

#### SDK Setup Logic

```
_setup_sdk(config):
    Check if opentelemetry.sdk is importable
        -> If not, raise ArcLLMConfigError("OTel module enabled but SDK not installed. Run: pip install arcllm[otel]")

    Build Resource(service.name=service_name, + resource_attributes)

    Select exporter:
        "otlp" -> OTLPSpanExporter(endpoint, protocol, headers, TLS settings)
        "console" -> ConsoleSpanExporter()
        "none" -> No exporter (spans exist but not exported)

    Build sampler:
        TraceIdRatioBasedSampler(sample_rate)

    Build processor:
        BatchSpanProcessor(exporter, max_batch_size, max_queue, schedule_delay, timeout)

    Create TracerProvider(resource, sampler)
    Add span processor
    Set as global TracerProvider
```

#### invoke() Logic

```
invoke(messages, tools, **kwargs):
    with self._span("arcllm.invoke") as span:
        # Pre-call attributes (GenAI semantic conventions)
        span.set_attribute("gen_ai.system", self._inner.name)
        span.set_attribute("gen_ai.request.model", self._inner.model_name)

        response = await self._inner.invoke(messages, tools, **kwargs)

        # Post-call attributes
        span.set_attribute("gen_ai.response.model", response.model)
        span.set_attribute("gen_ai.response.finish_reasons", [response.stop_reason])
        span.set_attribute("gen_ai.usage.input_tokens", response.usage.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", response.usage.output_tokens)

        return response
```

#### Config Keys

```toml
[modules.otel]
enabled = false
exporter = "otlp"                     # "otlp" | "console" | "none"
endpoint = "http://localhost:4317"
protocol = "grpc"                     # "grpc" | "http"
service_name = "arcllm"
sample_rate = 1.0

# Authentication
headers = {}

# TLS
insecure = false
certificate_file = ""
client_key_file = ""
client_cert_file = ""

# Export tuning
timeout_ms = 10000
max_batch_size = 512
max_queue_size = 2048
schedule_delay_ms = 5000

# Resource attributes
[modules.otel.resource_attributes]
```

### 3. RetryModule Changes (`modules/retry.py`)

Add span creation wrapping retry logic.

```
invoke(messages, tools, **kwargs):
    with self._span("arcllm.retry", {"arcllm.retry.max_attempts": self._max_retries}) as retry_span:
        for attempt in range(self._max_retries + 1):
            with self._span("arcllm.retry.attempt", {"arcllm.retry.attempt": attempt + 1}) as attempt_span:
                try:
                    result = await self._inner.invoke(messages, tools, **kwargs)
                    return result
                except ... as e:
                    if not retryable: raise
                    # Record exception but DON'T set ERROR (it's handled)
                    attempt_span.record_exception(e)
                    attempt_span.set_status(StatusCode.OK)  # Handled by retry
                    ...
        # All retries exhausted — ERROR on retry span
        retry_span.set_status(StatusCode.ERROR, "All retries exhausted")
        raise last_error
```

### 4. FallbackModule Changes (`modules/fallback.py`)

Add span creation wrapping fallback chain.

```
invoke(messages, tools, **kwargs):
    with self._span("arcllm.fallback") as fallback_span:
        try:
            return await self._inner.invoke(messages, tools, **kwargs)
        except Exception as primary_error:
            fallback_span.add_event("primary_failed", {"error": str(primary_error)})
            for provider_name in self._chain:
                with self._span("arcllm.fallback.attempt", {"arcllm.fallback.provider": provider_name}):
                    try:
                        fallback = load_model(provider_name)
                        result = await fallback.invoke(messages, tools, **kwargs)
                        return result
                    except ...:
                        continue
            fallback_span.set_status(StatusCode.ERROR, "All fallbacks exhausted")
            raise primary_error
```

### 5. RateLimitModule Changes (`modules/rate_limit.py`)

Add span creation with wait time attribute.

```
invoke(messages, tools, **kwargs):
    with self._span("arcllm.rate_limit") as span:
        wait = await self._bucket.acquire()
        span.set_attribute("arcllm.rate_limit.wait_ms", round(wait * 1000, 1))
        if wait > 0:
            span.add_event("throttled", {"wait_seconds": wait})
            logger.warning(...)
        return await self._inner.invoke(messages, tools, **kwargs)
```

### 6. TelemetryModule Changes (`modules/telemetry.py`)

Add span creation. Span captures same data as log line.

```
invoke(messages, tools, **kwargs):
    with self._span("arcllm.telemetry") as span:
        start = time.monotonic()
        response = await self._inner.invoke(messages, tools, **kwargs)
        elapsed = time.monotonic() - start
        cost = self._calculate_cost(response.usage)

        span.set_attribute("arcllm.telemetry.duration_ms", round(elapsed * 1000, 1))
        span.set_attribute("arcllm.telemetry.cost_usd", cost)

        # Existing log line (unchanged)
        log_structured(...)
        return response
```

### 7. AuditModule Changes (`modules/audit.py`)

Add span creation. Span captures audit metadata.

```
invoke(messages, tools, **kwargs):
    with self._span("arcllm.audit") as span:
        response = await self._inner.invoke(messages, tools, **kwargs)

        span.set_attribute("arcllm.audit.message_count", len(messages))
        span.set_attribute("arcllm.audit.content_length", len(response.content) if response.content else 0)
        if tools:
            span.set_attribute("arcllm.audit.tools_provided", len(tools))
        if response.tool_calls:
            span.set_attribute("arcllm.audit.tool_calls", len(response.tool_calls))

        # Existing log line (unchanged)
        log_structured(...)
        return response
```

### 8. Registry Changes (`registry.py`)

Add `otel=` kwarg, stack as outermost.

```python
def load_model(
    provider: str,
    model: str | None = None,
    *,
    retry: bool | dict | None = None,
    fallback: bool | dict | None = None,
    rate_limit: bool | dict | None = None,
    telemetry: bool | dict | None = None,
    audit: bool | dict | None = None,
    otel: bool | dict | None = None,  # NEW
) -> LLMProvider:
```

Stacking order (innermost first):
```
RateLimit -> Fallback -> Retry -> Audit -> Telemetry -> Otel -> return
```

### 9. pyproject.toml Changes

```toml
dependencies = [
    "pydantic>=2.0",
    "httpx>=0.25",
    "opentelemetry-api>=1.20",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "pytest-asyncio>=0.21",
]
otel = [
    "opentelemetry-sdk>=1.20",
    "opentelemetry-exporter-otlp-proto-grpc>=1.20",
    "opentelemetry-exporter-otlp-proto-http>=1.20",
]
```

---

## ADRs

### ADR-029: Deep OTel Integration in BaseModule

**Context**: Need to decide how deeply OTel integrates — wrapper module only, or baked into every module.

**Decision**: Deep integration. BaseModule provides `_tracer` and `_span()`, every module creates child spans.

**Rationale**: Full trace waterfall (retry attempts, fallback hops, rate-limit waits) is the primary value proposition. With 10,000 agents, operators need to see exactly where latency occurs within the ArcLLM stack. The `opentelemetry-api` is designed for this — zero-cost no-op when SDK not configured.

**Alternatives rejected**:
- OTel Module only (single span per invoke) — insufficient visibility
- OTel Module + optional context propagation — less detailed, still requires module awareness

### ADR-030: opentelemetry-api as Core Dependency

**Context**: Should `opentelemetry-api` be a core or optional dependency?

**Decision**: Core dependency.

**Rationale**: ~100KB, zero transitive dependencies, designed for library authors. Deep integration means every module needs the API. Import guards on every module would add complexity. The API is a no-op without the SDK — zero runtime cost.

**Alternatives rejected**:
- Optional with import guard — more code, import guards in every module

### ADR-031: OtelModule Root Span with Auto-Nesting

**Context**: Who creates the root span? Library or application?

**Decision**: OtelModule creates root span. When parent context exists (from agent framework), it automatically becomes a child span via OTel context propagation.

**Rationale**: Gives standalone users traces without any setup. When the BlackArc agent framework provides parent context, ArcLLM spans auto-nest. OTel handles this transparently via `start_as_current_span`.

**Alternatives rejected**:
- Library spans only (no root) — requires SDK setup for standalone use
- Auto-span in BaseModule — root name depends on stacking order

### ADR-032: GenAI Semantic Conventions + Custom Attributes

**Context**: What attributes go on OTel spans?

**Decision**: Standard `gen_ai.*` attributes where conventions exist, custom `arcllm.*` for ArcLLM-specific data.

**Rationale**: GenAI conventions are stable in OTel and auto-detected by Datadog, Grafana, AWS X-Ray for GenAI dashboards. Custom `arcllm.*` attributes cover cost calculation, retry details, rate-limit metrics that have no standard convention.

**Alternatives rejected**:
- Custom only — misses vendor dashboard integration
- Standard only — no room for ArcLLM-specific data

### ADR-033: Config-Driven SDK Setup

**Context**: How should the OTel SDK be configured?

**Decision**: TOML-driven setup in `[modules.otel]` config section.

**Rationale**: Consistent with all other ArcLLM configuration. Operators configure exporter, endpoint, sampling, TLS, and batch tuning via the same TOML files used for everything else. SDK packages are optional extras — clear error message if enabled but not installed.

**Alternatives rejected**:
- No SDK helpers (pure library) — requires consumer setup code
- Convenience function only — inconsistent with config-driven pattern

### ADR-034: Exception Events, ERROR Only on Final Failure

**Context**: How should errors appear on OTel spans?

**Decision**: Record exceptions as span events, only mark span status as ERROR on final failure.

**Rationale**: In trace UIs, ERROR status shows as red. If every retry attempt shows red, it creates noise for expected behavior. Recording the exception as an event preserves the data for debugging while keeping the trace clean. Root span only turns red when the operation truly failed.

**Alternatives rejected**:
- Mark each failure as ERROR — visual noise in trace UIs

### ADR-035: Enterprise TLS and Batch Configuration

**Context**: What config knobs does the OTel module need?

**Decision**: Full enterprise config: auth headers, TLS (insecure, CA cert, client cert/key), batch tuning (size, queue, delay, timeout), resource attributes.

**Rationale**: Federal production requires mTLS for trace export. 10,000 agents generate high span volume requiring batch tuning. Resource attributes enable deployment-level filtering in dashboards.

**Alternatives rejected**:
- Minimal config (endpoint only) — insufficient for federal/enterprise
- Basic + no TLS — blocks federal deployment

---

## Edge Cases

| Case | Handling |
|------|----------|
| OTel SDK not installed, module enabled | `ArcLLMConfigError` with install instructions |
| OTel SDK not installed, module disabled | No error — API no-ops silently |
| No parent span context (standalone use) | OtelModule span becomes root automatically |
| Parent span exists (agent framework) | OtelModule span becomes child automatically |
| Exporter endpoint unreachable | OTel SDK handles gracefully (logs warning, drops spans) |
| sample_rate = 0.0 | No traces exported (all sampled out) |
| sample_rate = 1.0 | All traces exported |
| sample_rate out of range | `ArcLLMConfigError` |
| Invalid exporter type | `ArcLLMConfigError` |
| Invalid protocol | `ArcLLMConfigError` |
| Empty certificate_file path | Skip TLS cert configuration |
| Invalid certificate path | Let OTel SDK raise its own error (pass through) |
| Inner provider raises exception | Span records exception, sets ERROR, re-raises |
| Retry succeeds on attempt 2 | Attempt 1 span: exception event + OK status. Attempt 2 span: OK. Root: OK. |
| All retries fail | All attempt spans: exception events + OK. Retry span: ERROR. Root: ERROR. |
| Fallback succeeds | Primary attempt: exception event. Fallback span: OK. Root: OK. |
| All fallbacks fail | All attempts: exception events. Fallback span: ERROR. Root: ERROR. |
| Rate-limit with zero wait | Span has `wait_ms=0.0`, no throttled event |
| Rate-limit with wait | Span has `wait_ms=200.0`, throttled event added |
| otel=False kwarg | Disables even if config.toml has enabled=true |
| otel={"exporter": "console"} dict | Overrides config.toml defaults |
| Multiple load_model() calls | SDK setup happens once (TracerProvider is global) |
| Config key validation | Unknown keys rejected with ArcLLMConfigError |

---

## Test Strategy

Three new test files + additions to test_registry.py.

| File | Tests | Priority |
|------|-------|----------|
| `test_otel.py` | OtelModule + SDK setup (~25 tests) | P0 |
| `test_base_module.py` | BaseModule _tracer/_span() (~10 tests) | P0 |
| `test_otel_integration.py` | Module span creation (~15 tests) | P1 |
| `test_registry.py` (additions) | OTel stacking (~5 tests) | P0 |

### Key Test Scenarios

**BaseModule (_tracer, _span):**
- `_tracer` returns a Tracer (or no-op Tracer)
- `_span()` creates a span with given name
- `_span()` records exception and sets ERROR on unhandled error
- `_span()` re-raises exceptions (transparent)
- `_span()` accepts attributes dict
- Nested `_span()` calls create parent-child relationship
- `_span()` is no-op when no SDK configured (verify no crash)

**OtelModule Core:**
- Creates `arcllm.invoke` span
- Sets `gen_ai.system` attribute from inner.name
- Sets `gen_ai.request.model` from inner.model_name
- Sets `gen_ai.usage.input_tokens` from response
- Sets `gen_ai.usage.output_tokens` from response
- Sets `gen_ai.response.finish_reasons` from response
- Sets `gen_ai.response.model` from response
- Delegates to inner provider (messages, tools, kwargs pass through)
- Returns response unchanged (same object reference)
- Auto-nests under parent span when present

**OtelModule Config Validation:**
- Invalid exporter type rejected
- Invalid protocol rejected
- sample_rate < 0 rejected
- sample_rate > 1 rejected
- Unknown config keys rejected
- SDK not installed + enabled = ArcLLMConfigError with install instructions

**OtelModule SDK Setup:**
- OTLP exporter created with endpoint and protocol
- Console exporter created
- None exporter = no exporter added
- Auth headers passed to exporter
- TLS settings passed to exporter
- Batch processor configured with tuning params
- Resource includes service_name and custom attributes
- Sampler created with sample_rate
- Setup idempotent (second call does not reconfigure)

**Module Span Integration:**
- RetryModule creates arcllm.retry span
- RetryModule creates arcllm.retry.attempt child spans
- RetryModule records exception on failed attempt
- RetryModule marks attempt OK when error handled
- RetryModule marks retry span ERROR on final failure
- FallbackModule creates arcllm.fallback span
- FallbackModule creates per-provider child spans
- RateLimitModule creates arcllm.rate_limit span with wait_ms
- TelemetryModule creates arcllm.telemetry span
- AuditModule creates arcllm.audit span with message_count

**Registry Integration:**
- load_model with otel=True wraps with OtelModule
- OTel outermost in full stack
- otel=False overrides config.toml
- otel dict merges with config.toml defaults

### Testing Approach

Tests mock OTel API to avoid requiring the SDK:
- Use `unittest.mock.patch` on `trace.get_tracer()` to return a mock Tracer
- Mock tracer returns mock Spans that record `set_attribute`, `add_event`, `set_status`, `record_exception` calls
- Verify attributes set on spans without any real OTel infrastructure
- OTel SDK setup tests use `try/except ImportError` mocking
