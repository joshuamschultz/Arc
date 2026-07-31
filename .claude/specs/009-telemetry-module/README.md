# Spec: 009-telemetry-module

## Metadata

| Field | Value |
|-------|-------|
| **ID** | 009 |
| **Name** | Telemetry Module |
| **Type** | Library/Backend |
| **Status** | COMPLETE |
| **Created** | 2026-02-08 |
| **Confidence** | High (>70%) — Module pattern established in 007-008, all decisions made |

## Summary

Implements the TelemetryModule for ArcLLM using structured logging of timing, token usage, and cost per `invoke()` call. The module sits outermost in the stack (`Telemetry(Retry(Fallback(RateLimit(adapter))))`), measuring total wall-clock duration including retries, fallback attempts, and rate-limit waits. Cost is calculated from per-1M token pricing stored in provider TOML model metadata and automatically injected by `load_model()` via `setdefault()`. Cache tokens (read/write) are conditionally included when present. Log level is configurable (default INFO).

## Source

ArcLLM Build Step 10. Decisions made interactively via `/build-arcllm 10` session.

## Decisions Log

| Decision | Choice | Rationale | Date |
|----------|--------|-----------|------|
| D-064 Output | Structured logging only — no callback, no accumulator | Simple, toggle-able via config, no functions in load_model() config. Budget and OTel handle their own concerns. | 2026-02-08 |
| D-065 Cost | Calculate and log cost_usd per call from provider pricing metadata | Pricing already in provider TOML; cost per call is essential for budget tracking and ops visibility. | 2026-02-08 |
| D-066 Pricing injection | load_model() injects pricing from ProviderConfig model metadata into telemetry config dict via setdefault() | Pricing lives in provider TOML, TelemetryModule shouldn't know about ProviderConfig. setdefault() allows explicit overrides. | 2026-02-08 |
| D-067 Stack order | Outermost: Telemetry(Retry(Fallback(RateLimit(adapter)))) | Measures total wall-clock including retries, fallback, and rate-limit wait. Most useful operational metric. | 2026-02-08 |
| D-068 Log level | INFO by default, configurable via log_level in config dict | Telemetry should be visible when enabled. Configurable for noisy environments. | 2026-02-08 |
| D-069 Log fields | provider, model, duration_ms, input_tokens, output_tokens, total_tokens, cache_read/write_tokens (conditional), cost_usd, stop_reason | Complete operational visibility per call. Cache tokens omitted when absent to reduce noise. | 2026-02-08 |

## Learnings

- Pricing injection via `setdefault()` is elegant — provider TOML has the cost data, `load_model()` bridges it to TelemetryModule config, and explicit overrides still win.
- Cache token fields should be conditional (only logged when not None) to reduce log noise for providers that don't use caching.
- `time.monotonic()` for wall-clock measurement — immune to system clock adjustments, already battle-tested in RateLimitModule.
- Cost formula `(tokens * cost_per_1m) / 1_000_000` is straightforward but needed pytest.approx for floating-point comparison in tests.
- 100 lines of implementation is the right size for a single-concern module — readable, testable, maintainable.
- **Review fix**: Created shared `_logging.py` with `log_structured()` + `_sanitize()` to prevent log injection and provide consistent structured logging across all modules.
- **Review fix**: Added `_VALID_CONFIG_KEYS` strict validation — catches typo'd config keys at construction instead of silently ignoring them.
- **Review fix**: 7 additional edge case tests (negative cache costs, exception propagation, cache 0 vs None, model name sanitization, unknown config keys).

## Cross-References

- PRD: `PRD.md` (this directory)
- SDD: `SDD.md` (this directory)
- PLAN: `PLAN.md` (this directory)
- Step 7 Spec: `.claude/specs/007-module-system-retry-fallback/`
- Step 8 Spec: `.claude/specs/008-rate-limiter/`
- Registry: `src/arcllm/registry.py`
- BaseModule: `src/arcllm/modules/base.py`
- Implementation: `src/arcllm/modules/telemetry.py`
- Tests: `tests/test_telemetry.py`
- Config: `src/arcllm/config.toml` (`[modules.telemetry]`)
- Provider Pricing: `src/arcllm/providers/anthropic.toml` (cost_* fields in model metadata)
- Product PRD: `docs/arcllm-prd.md`
- Decision Log: `.claude/decision-log.md`
- Steering: `.claude/steering/`
