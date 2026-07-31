# PRD: Telemetry Module

> Feature-specific requirements for ArcLLM Step 10.
> References steering docs in `.claude/steering/`.

---

## Feature Overview

### Problem Statement

At scale (thousands of concurrent agents), operators need visibility into LLM call performance, token consumption, and cost. Without telemetry, debugging slow calls requires adding ad-hoc logging in each agent, token usage is invisible until the monthly bill arrives, and cost attribution per model/provider is impossible. There is currently no mechanism in ArcLLM to measure and report per-call metrics.

### Goal

1. **Implement a TelemetryModule** that wraps `invoke()` to log wall-clock timing, token usage, and calculated cost in a single structured log line per call.
2. **Calculate cost automatically** from provider model metadata pricing (cost_per_1M tokens) stored in provider TOML files.
3. **Inject pricing transparently** via `load_model()` — TelemetryModule receives pricing through its config dict without knowing about ProviderConfig.
4. **Integrate as the outermost module** in the stack: `Telemetry(Retry(Fallback(RateLimit(adapter))))` to capture total wall-clock including retries, fallbacks, and rate-limit waits.
5. **Support conditional cache token logging** — include cache_read_tokens and cache_write_tokens only when present (providers like Anthropic support prompt caching).

### Success Criteria

- `TelemetryModule` wraps any `LLMProvider` and logs structured metrics after each `invoke()` call
- Wall-clock duration measured via `time.monotonic()` (immune to clock adjustments)
- Cost calculated as `(tokens * cost_per_1m) / 1_000_000` for input, output, cache_read, cache_write
- Pricing automatically injected from provider TOML model metadata by `load_model()`
- `setdefault()` injection allows explicit cost overrides to take precedence
- Cache token fields conditionally included (only when not None)
- `load_model("anthropic", telemetry=True)` enables telemetry with config.toml defaults
- `load_model("anthropic", telemetry={"log_level": "DEBUG"})` overrides config
- Stacking order: `Telemetry(Retry(Fallback(RateLimit(adapter))))` — telemetry outermost
- Configurable log level (default INFO), validated at construction
- All existing 245 tests pass unchanged
- New telemetry tests fully mocked (no real API calls, no real time waits)
- Zero new dependencies (uses stdlib `time`, `logging`)

---

## Requirements

### Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|------------|----------|------------|
| FR-1 | `TelemetryModule` wraps invoke() and logs after each call | P0 | Structured log line emitted with all configured fields |
| FR-2 | Wall-clock timing via `time.monotonic()` start/end | P0 | `duration_ms` field in log, rounded to 1 decimal |
| FR-3 | Token usage from `LLMResponse.usage` (input, output, total) | P0 | `input_tokens`, `output_tokens`, `total_tokens` in log |
| FR-4 | Cache tokens conditional: only log when present | P0 | `cache_read_tokens` and `cache_write_tokens` omitted when None |
| FR-5 | Cost calculation: `(tokens * cost_per_1m) / 1_000_000` | P0 | `cost_usd` field with 6 decimal precision |
| FR-6 | Cost includes all 4 token types (input, output, cache_read, cache_write) | P0 | Full cost accounting when all token types present |
| FR-7 | Config keys: `cost_input_per_1m`, `cost_output_per_1m`, `cost_cache_read_per_1m`, `cost_cache_write_per_1m` | P0 | All default to 0.0 if not provided |
| FR-8 | Pricing injection by `load_model()` from `ProviderConfig.models[model_name]` metadata | P0 | Uses `setdefault()` — explicit overrides win |
| FR-9 | Configurable log level via `log_level` config key | P0 | Default "INFO", validated against standard Python levels |
| FR-10 | Invalid log level raises `ArcLLMConfigError` | P0 | Fail-fast validation at construction |
| FR-11 | Negative cost values raise `ArcLLMConfigError` | P0 | Fail-fast validation at construction |
| FR-12 | Registry integration: `telemetry=` kwarg on `load_model()` | P0 | Same 4-level resolution as retry/fallback/rate_limit |
| FR-13 | Stack order: telemetry outermost | P0 | Applied after retry, which is after fallback, which is after rate_limit |
| FR-14 | Log includes `provider` and `model` identifiers | P1 | For filtering/alerting in log aggregation systems |
| FR-15 | Log includes `stop_reason` | P1 | Distinguishes end_turn from tool_use calls |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|------------|-----------|
| NFR-1 | Telemetry overhead (logging + cost math) | <1ms per call (calls take 500-5000ms) |
| NFR-2 | Zero new dependencies | Uses stdlib `time`, `logging` |
| NFR-3 | All tests run without real time waits | Mocked `time.monotonic` |
| NFR-4 | Existing 245 tests unaffected | Zero regressions |
| NFR-5 | Module code independently testable | No adapter setup needed |
| NFR-6 | Cost precision | 6 decimal places (sub-cent accuracy) |

---

## User Stories

### Agent Developer

> As an agent developer, I want `load_model("anthropic", telemetry=True)` to log timing and cost per call so I can monitor my agent's LLM usage without instrumenting it myself.

### Platform Engineer

> As a platform engineer, I want telemetry logging to automatically pick up pricing from provider TOML metadata so cost reporting works out-of-the-box without per-agent configuration.

### Operations

> As an ops engineer, I want structured log lines with provider, model, duration, tokens, and cost so I can build dashboards, set alerts on latency/cost, and attribute costs per model.

### Finance

> As a finance stakeholder, I want per-call cost tracking so I can verify monthly bills, identify cost-heavy models, and make informed decisions about model selection.

---

## Out of Scope (Step 10)

- In-memory accumulator for aggregating telemetry across calls
- Callback functions for custom telemetry consumers
- OpenTelemetry spans (Step 13)
- Budget enforcement based on telemetry data (Step 12)
- Distributed tracing correlation IDs
- Per-message or per-tool-call granularity (module operates at invoke() level)
- Telemetry for streaming responses (not yet supported)

---

## Personas Referenced

- **Agent Developer** (primary) — see `steering/product.md`
- **Platform Engineer** (secondary) — see `steering/product.md`
- **Operations** (tertiary) — see `steering/product.md`

---

## Dependencies

| Dependency | Type | Status |
|------------|------|--------|
| Step 1-8 (Core + Modules + Rate Limiter) | Prerequisite | COMPLETE |
| `BaseModule` | Base class | Defined in modules/base.py |
| `LLMProvider` ABC | Interface | Defined in types.py |
| `Usage` type | Data model | Defined in types.py (has cache_read/write_tokens) |
| `load_model()` registry | Integration point | Has module stacking from Step 7-8 |
| `config.toml [modules.telemetry]` | Config | Has `enabled = false`, `log_level = "INFO"` |
| `ProviderConfig.models[].cost_*` | Pricing source | Model metadata in provider TOMLs |
| `ModelMetadata` | Config model | Has cost_input_per_1m, cost_output_per_1m, cost_cache_read_per_1m, cost_cache_write_per_1m |
| `ArcLLMConfigError` | Exception type | Defined in exceptions.py |
