# PRD: Rate Limiter Module

> Feature-specific requirements for ArcLLM Step 8.
> References steering docs in `.claude/steering/`.

---

## Feature Overview

### Problem Statement

At scale (thousands of concurrent agents sharing an API key), unthrottled outgoing requests overwhelm provider rate limits, causing 429 storms. Each 429 triggers retry + backoff, but synchronized retries create a thundering herd that amplifies the problem. Rate limiting *before* the call is cheaper than retrying *after* a 429. There is currently no mechanism in ArcLLM to throttle outgoing request rate.

### Goal

1. **Implement a token bucket rate limiter** that throttles requests per provider before they hit the API.
2. **Share rate limiter state per provider** — all agents using the same API key share one bucket, matching how provider rate limits actually work.
3. **Integrate with the module stack** as the innermost wrapper: `Retry(Fallback(RateLimit(adapter)))`.
4. **Async-wait when throttled** with WARNING logs so operators can diagnose slow calls.

### Success Criteria

- `TokenBucket` class implements token bucket algorithm with configurable capacity and refill rate
- `RateLimitModule` wraps any `LLMProvider` and acquires a token before each `invoke()` call
- Token buckets are shared per-provider across all `load_model()` instances
- When bucket is empty, caller `await`s until a token refills (no error raised)
- WARNING log emitted when a call is throttled, including provider name and wait time
- `load_model("anthropic", rate_limit=True)` enables rate limiting
- `load_model("anthropic", rate_limit={"requests_per_minute": 120})` overrides config
- Stacking order: `Retry(Fallback(RateLimit(adapter)))` — rate limit is innermost module
- `clear_buckets()` resets shared state; hooked into `registry.clear_cache()`
- All existing 219 tests pass unchanged
- New rate limit tests are fully mocked (no real API calls, no real time waits)
- Zero new dependencies (uses stdlib `asyncio`, `time`)

---

## Requirements

### Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|------------|----------|------------|
| FR-1 | `TokenBucket` with configurable capacity and refill rate | P0 | Bucket fills at `RPM/60` tokens/sec, capped at `burst_capacity` |
| FR-2 | `TokenBucket.acquire()` consumes one token, waits if empty | P0 | Returns wait time (0.0 if no wait) |
| FR-3 | `TokenBucket` uses `asyncio.Lock` for concurrent safety | P0 | Multiple coroutines sharing one bucket don't corrupt state |
| FR-4 | `RateLimitModule` acquires token before delegating to inner | P0 | Token consumed per `invoke()` call |
| FR-5 | Per-provider shared buckets via module-level registry | P0 | Same provider name = same bucket instance |
| FR-6 | WARNING log when caller must wait for a token | P0 | Includes provider name and wait duration |
| FR-7 | `clear_buckets()` resets all shared bucket state | P0 | Test isolation |
| FR-8 | `registry.clear_cache()` calls `clear_buckets()` | P0 | Automatic cleanup |
| FR-9 | Registry integration: `rate_limit=` kwarg on `load_model()` | P0 | Same 4-level resolution as retry/fallback |
| FR-10 | Stack order: rate_limit innermost (wraps adapter directly) | P0 | Applied before fallback and retry |
| FR-11 | Config validation: RPM > 0, burst_capacity >= 1 | P0 | `ArcLLMConfigError` on invalid values |
| FR-12 | `burst_capacity` defaults to `requests_per_minute` if not specified | P1 | Sensible default |
| FR-13 | Token refill uses `time.monotonic()` for clock safety | P1 | No wall-clock drift issues |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|------------|-----------|
| NFR-1 | Token acquisition overhead (no wait) | <0.1ms |
| NFR-2 | Zero new dependencies | Uses stdlib `asyncio`, `time` |
| NFR-3 | All tests run without real time waits | Mocked `asyncio.sleep` and `time.monotonic` |
| NFR-4 | Existing 219 tests unaffected | Zero regressions |
| NFR-5 | Module code independently testable | No adapter setup needed |

---

## User Stories

### Agent Developer

> As an agent developer, I want `load_model("anthropic", rate_limit=True)` to throttle my outgoing requests so I don't have to implement rate limiting in every agent, and so I get fewer 429 errors.

### Platform Engineer

> As a platform engineer, I want to set a global `requests_per_minute = 60` in config.toml so that all agents respect our API rate limit without any per-agent configuration.

### Operations

> As an ops engineer, I want to see WARNING logs when agents are being throttled so I can identify capacity issues and consider upgrading our API tier.

---

## Out of Scope (Step 8)

- Per-model rate limiting (all models on a provider share one bucket)
- Distributed rate limiting across processes (in-process only)
- Adaptive rate limiting (monitoring 429s to auto-adjust limits)
- Token-based rate limiting (counting input/output tokens, not requests)
- Rate limit headers from response (Anthropic/OpenAI send remaining-requests headers)

---

## Personas Referenced

- **Agent Developer** (primary) — see `steering/product.md`
- **Platform Engineer** (secondary) — see `steering/product.md`

---

## Dependencies

| Dependency | Type | Status |
|------------|------|--------|
| Step 1-7 (Core + Modules) | Prerequisite | COMPLETE |
| `BaseModule` | Base class | Defined in modules/base.py |
| `LLMProvider` ABC | Interface | Defined in types.py |
| `load_model()` registry | Integration point | Has module stacking from Step 7 |
| `config.toml [modules.rate_limit]` | Config | Already has `requests_per_minute = 60` |
| `ArcLLMConfigError` | Exception type | Defined in exceptions.py |
