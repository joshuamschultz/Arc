# Spec: 008-rate-limiter

## Metadata

| Field | Value |
|-------|-------|
| **ID** | 008 |
| **Name** | Rate Limiter Module |
| **Type** | Library/Backend |
| **Status** | PENDING |
| **Created** | 2026-02-08 |
| **Confidence** | High (>70%) — Module pattern established in 007, all decisions made |

## Summary

Implements the RateLimitModule for ArcLLM using a token bucket algorithm with per-provider shared state. The module sits innermost in the stack (`Retry(Fallback(RateLimit(adapter)))`), throttling outgoing requests before they hit the provider API. When the bucket is empty, it async-waits for a token and emits a WARNING log so operators know why calls are slower. Token buckets are shared across all `load_model()` instances for the same provider, matching how provider rate limits actually work (per API key).

## Source

ArcLLM Build Step 8. Decisions made interactively via `/build-arcllm 8` session.

## Decisions Log

| Decision | Choice | Rationale | Date |
|----------|--------|-----------|------|
| D-055 Algorithm | Token bucket | Allows bursts, enforces average rate. Battle-tested (nginx, AWS). Simple: one counter + timestamp. | 2026-02-08 |
| D-056 Scope | Per-provider (shared bucket) | Matches provider reality: rate limits are per API key, not per model. All agents share one bucket. | 2026-02-08 |
| D-057 Shared state | Module-level registry in rate_limit.py | Consistent with config cache pattern in registry.py. Dict maps provider name to TokenBucket. | 2026-02-08 |
| D-058 Behavior | Async wait + WARNING log | Transparent to agents (no error). WARNING log explains why calls are slower. | 2026-02-08 |
| D-059 Burst config | Separate burst_capacity param | Defaults to requests_per_minute. Overridable for fine-tuning burst behavior. | 2026-02-08 |
| D-060 Stack order | Innermost: Retry(Fallback(RateLimit(adapter))) | Throttles before call goes out. Fallback doesn't trigger on rate-limit waits. Retry covers failures after rate-limited call. | 2026-02-08 |
| D-061 Provider ID | Read inner.name at construction | Already available on all LLMProvider instances. No extra config needed. | 2026-02-08 |
| D-062 Validation | RPM > 0, burst >= 1 | Zero RPM = no requests ever. Zero burst = can't make any request. Fail-fast (D-032). | 2026-02-08 |
| D-063 Test cleanup | clear_buckets() + hook into registry.clear_cache() | Follows existing pattern. Tests using clear_cache() automatically get clean rate limit state. | 2026-02-08 |

## Learnings

(To be filled during implementation)

## Cross-References

- PRD: `PRD.md` (this directory)
- SDD: `SDD.md` (this directory)
- PLAN: `PLAN.md` (this directory)
- Step 7 Spec: `.claude/specs/007-module-system-retry-fallback/`
- Registry: `src/arcllm/registry.py`
- BaseModule: `src/arcllm/modules/base.py`
- Config: `src/arcllm/config.toml` (`[modules.rate_limit]`)
- Product PRD: `docs/arcllm-prd.md`
- Decision Log: `.claude/decision-log.md`
- Steering: `.claude/steering/`
