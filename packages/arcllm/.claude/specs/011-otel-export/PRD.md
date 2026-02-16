# PRD: OpenTelemetry Export

> Feature-specific requirements for ArcLLM Step 13.
> References steering docs in `.claude/steering/`.

---

## Feature Overview

### Problem Statement

ArcLLM's current observability is structured logging only (TelemetryModule for timing/tokens/cost, AuditModule for compliance metadata). While effective for basic ops, this approach cannot answer critical production questions at scale: "What was the full execution path of request abc-123 through retry, fallback, and rate-limit layers?" "Which layer added the most latency?" "What percentage of calls are hitting fallback?" With 10,000 concurrent agents, operators need distributed tracing — correlated, hierarchical spans that show the complete call waterfall — not just individual log lines. Additionally, federal compliance (NIST 800-53 AU-3, AU-12) is better satisfied with structured, correlated traces than grep-able log lines. OpenTelemetry is the industry-standard protocol supported by all major observability vendors (Jaeger, Datadog, Grafana Tempo, AWS X-Ray).

### Goal

1. **Add `opentelemetry-api` as a core dependency** — zero-cost no-op when SDK not configured, always available for span creation.
2. **Add `_tracer` and `_span()` helper to BaseModule** — every module creates child spans automatically, giving a full trace waterfall.
3. **Implement OtelModule** — outermost wrapper that creates the root `arcllm.invoke` span with GenAI semantic convention attributes plus custom `arcllm.*` attributes.
4. **Update all existing modules** (retry, fallback, rate_limit, telemetry, audit) to create child spans using the BaseModule helper.
5. **Config-driven SDK setup** via `[modules.otel]` TOML section — supports OTLP, console, and none exporters with enterprise features (auth headers, TLS/mTLS, batch tuning, sampling, resource attributes).
6. **Add optional `otel` extras** to `pyproject.toml` — `pip install arcllm[otel]` installs `opentelemetry-sdk` and OTLP exporter.

### Success Criteria

- `opentelemetry-api` added to core dependencies in `pyproject.toml`
- `opentelemetry-sdk` and OTLP exporter available as `arcllm[otel]` optional extras
- `BaseModule` provides `self._tracer` (OTel Tracer) and `self._span(name)` context manager
- `OtelModule` creates root span `arcllm.invoke` with GenAI semantic attributes (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.*`, etc.)
- `OtelModule` adds custom attributes (`arcllm.cost_usd`, etc.)
- `OtelModule` auto-nests under parent span when agent framework provides one
- All existing modules create child spans (`arcllm.retry`, `arcllm.audit`, etc.)
- RetryModule creates per-attempt child spans with attempt number
- RetryModule records exceptions on attempt spans but only marks root as ERROR on final failure
- FallbackModule creates per-provider child spans
- RateLimitModule records wait time as span attribute
- Config-driven setup: `[modules.otel]` in config.toml with exporter, endpoint, protocol, sample_rate, headers, TLS, batch tuning, resource_attributes
- `load_model("anthropic", otel=True)` enables OTel with defaults
- Clear `ArcLLMConfigError` if OTel enabled but SDK not installed
- Stacking order: `Otel(Telemetry(Audit(Retry(Fallback(RateLimit(adapter))))))`
- All existing 323 tests pass unchanged
- New OTel tests fully mocked (no real OTel SDK required for test suite)
- Zero performance impact when OTel not configured (no-op API)

---

## Requirements

### Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|------------|----------|------------|
| FR-1 | `opentelemetry-api` added as core dependency | P0 | `from opentelemetry import trace` works after `pip install arcllm` |
| FR-2 | `opentelemetry-sdk` + OTLP exporter as optional `otel` extras | P0 | `pip install arcllm[otel]` installs SDK and exporter |
| FR-3 | BaseModule provides `self._tracer` property | P0 | Returns OTel Tracer via `trace.get_tracer("arcllm")` |
| FR-4 | BaseModule provides `self._span(name)` context manager | P0 | Creates a span named `name`, yields the span, handles exceptions |
| FR-5 | OtelModule creates root span `arcllm.invoke` per call | P0 | Span visible in trace exporter |
| FR-6 | OtelModule sets GenAI semantic convention attributes on root span | P0 | `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons` |
| FR-7 | OtelModule sets custom `arcllm.*` attributes on root span | P0 | `arcllm.cost_usd` when telemetry pricing available |
| FR-8 | OtelModule auto-nests under parent span from agent framework | P0 | When parent context exists, root span becomes child |
| FR-9 | RetryModule creates `arcllm.retry` span wrapping all attempts | P0 | Shows total retry duration |
| FR-10 | RetryModule creates `arcllm.retry.attempt` child span per attempt | P0 | Each attempt has `arcllm.retry.attempt` (int) attribute |
| FR-11 | RetryModule records exception event on failed attempt spans | P0 | Exception details available in span events |
| FR-12 | RetryModule marks attempt span OK when error is handled (retried) | P0 | Clean trace UI for expected retries |
| FR-13 | RetryModule marks root span ERROR only on final failure | P0 | Only turns red when all retries exhausted |
| FR-14 | FallbackModule creates `arcllm.fallback` span wrapping all attempts | P1 | Shows total fallback duration |
| FR-15 | FallbackModule creates child span per fallback provider tried | P1 | `arcllm.fallback.provider` attribute on each |
| FR-16 | RateLimitModule creates `arcllm.rate_limit` span | P1 | Shows throttle duration |
| FR-17 | RateLimitModule sets `arcllm.rate_limit.wait_ms` attribute | P1 | Non-zero when throttled |
| FR-18 | TelemetryModule creates `arcllm.telemetry` span | P1 | Captures timing data as span |
| FR-19 | AuditModule creates `arcllm.audit` span | P1 | Captures audit metadata as span |
| FR-20 | Config-driven SDK setup via `[modules.otel]` TOML section | P0 | TracerProvider + exporter configured from TOML |
| FR-21 | Exporter types: `otlp`, `console`, `none` | P0 | Each creates appropriate exporter |
| FR-22 | OTLP endpoint and protocol (grpc/http) configurable | P0 | Connects to specified collector |
| FR-23 | Sample rate configurable (0.0-1.0) | P1 | `TraceIdRatioBased` sampler applied |
| FR-24 | Auth headers configurable for OTLP | P0 | Passed to OTLP exporter for secured endpoints |
| FR-25 | TLS settings: `insecure`, `certificate_file`, `client_key_file`, `client_cert_file` | P0 | mTLS support for federal/zero-trust networks |
| FR-26 | Batch tuning: `timeout_ms`, `max_batch_size`, `max_queue_size`, `schedule_delay_ms` | P1 | BatchSpanProcessor configured from TOML |
| FR-27 | Resource attributes configurable | P1 | Added to OTel Resource (deployment.environment, service.version, etc.) |
| FR-28 | `service_name` configurable | P0 | Sets `service.name` resource attribute |
| FR-29 | `load_model()` accepts `otel=` kwarg (same resolution pattern) | P0 | True/False/dict/None with config.toml merge |
| FR-30 | Clear error if OTel enabled but SDK not installed | P0 | `ArcLLMConfigError` with install instructions |
| FR-31 | Stacking: Otel outermost (wraps telemetry) | P0 | `Otel(Telemetry(Audit(Retry(Fallback(RateLimit(adapter))))))` |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|------------|-----------|
| NFR-1 | Zero overhead when OTel not configured | No-op tracer, no span creation cost |
| NFR-2 | Span creation overhead when configured | <0.1ms per span |
| NFR-3 | Core dep (opentelemetry-api) size | ~100KB, zero transitive deps |
| NFR-4 | All tests run without OTel SDK installed | Fully mocked, no-op API |
| NFR-5 | Existing 323 tests unaffected | Zero regressions |
| NFR-6 | Module independently testable | No OTel SDK setup needed for unit tests |
| NFR-7 | Federal compliance | NIST 800-53 AU-3, AU-12 compatible via structured traces |
| NFR-8 | mTLS support | Certificate-based auth for zero-trust networks |

---

## User Stories

### Platform Engineer

> As a platform engineer operating 10,000 agents, I want distributed tracing across all ArcLLM calls so I can see the full execution waterfall (retry, fallback, rate-limit, HTTP) in Jaeger/Grafana Tempo and quickly diagnose latency issues.

### Agent Developer

> As an agent developer, I want `load_model("anthropic", otel=True)` to automatically create traces so I get observability without writing OTel setup code in every agent.

### Compliance Officer

> As a compliance officer, I want correlated trace IDs linking audit records, telemetry data, and LLM calls so I can satisfy NIST 800-53 AU-3 requirements for audit record content and AU-12 for audit generation capabilities.

### Operations (SRE)

> As an SRE, I want to configure OTel export (endpoint, sampling, batch size) via TOML config so I can tune trace volume and export behavior without code changes across thousands of deployed agents.

### Security Engineer

> As a security engineer in a federal environment, I want mTLS support for the OTLP exporter so trace data is encrypted in transit and authenticated via client certificates in our zero-trust network.

### Agent Framework Developer

> As the developer of the BlackArc agent framework, I want ArcLLM's spans to automatically nest under my framework's root spans so I get end-to-end traces from agent task through LLM call without any integration code.

---

## Out of Scope (Step 13)

- OTel Metrics signal (counters, histograms) — future step
- OTel Logs signal (structured log export via OTel) — future step
- Custom span processors or exporters beyond OTLP/console
- Trace context propagation across HTTP headers (W3C Trace Context for inter-service)
- Span links (correlating related but non-parent-child spans)
- Dynamic sampling (adjusting sample rate based on error rate)
- Per-model or per-agent sampling rules
- OTel Baggage propagation
- Streaming response span events

---

## Personas Referenced

- **Platform Engineer** (primary) — see `steering/product.md`
- **Agent Developer** (secondary) — see `steering/product.md`
- **Compliance Officer** (tertiary) — see `steering/product.md`
- **Agent Framework Developer** (quaternary) — future BlackArc integration

---

## Dependencies

| Dependency | Type | Status |
|------------|------|--------|
| Steps 1-8, 10-11 (Core + All Modules) | Prerequisite | COMPLETE |
| `BaseModule` | Base class (will be modified) | Defined in modules/base.py |
| `LLMProvider` ABC | Interface | Defined in types.py |
| `LLMResponse` type | Response model | Has content, tool_calls, usage, model, stop_reason |
| `opentelemetry-api` | New core dependency | Will be added to pyproject.toml |
| `opentelemetry-sdk` | New optional dependency | Will be added as `otel` extra |
| `opentelemetry-exporter-otlp-proto-grpc` | New optional dependency | Will be added as `otel` extra |
| `opentelemetry-exporter-otlp-proto-http` | New optional dependency | Will be added as `otel` extra |
| `load_model()` registry | Integration point | Has module stacking from Steps 7-11 |
| `config.toml [modules.otel]` | Config | Will be added |
| `ArcLLMConfigError` | Exception type | Defined in exceptions.py |
