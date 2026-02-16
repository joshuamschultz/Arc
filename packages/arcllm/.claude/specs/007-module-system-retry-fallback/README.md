# Spec: 007-module-system-retry-fallback

## Metadata

| Field | Value |
|-------|-------|
| **ID** | 007 |
| **Name** | Module System + Retry + Fallback |
| **Type** | Library/Backend |
| **Status** | PENDING |
| **Created** | 2026-02-08 |
| **Confidence** | High (>70%) — Core foundation complete, all decisions made |

## Summary

Establishes the module system pattern for ArcLLM and implements the first two modules: RetryModule and FallbackModule. The module pattern uses wrapper classes that implement `LLMProvider` and stack around the adapter, intercepting `invoke()` calls. This sets the foundation for all future modules (rate limiter, telemetry, audit, budget, routing). The registry (`load_model()`) is updated to read module config and automatically stack enabled modules around the adapter.

## Source

ArcLLM Build Step 7. Decisions made interactively via `/build-arcllm 7` session.

## Decisions Log

| Decision | Choice | Rationale | Date |
|----------|--------|-----------|------|
| D-047 Module integration pattern | Wrapper classes (middleware) | Each module wraps invoke() with its own logic. Composable, testable, single-responsibility. Scales to 7+ modules without invoke() becoming a giant conditional. | 2026-02-08 |
| D-048 Retry vs Fallback structure | Two separate modules | Independently composable. Retry without fallback, or fallback without retry. Each file stays small and focused. | 2026-02-08 |
| D-049 Retry triggers | 429, 500, 502, 503, 529 + connection errors | Standard transient codes documented by Anthropic and OpenAI. 529 is Anthropic-specific overload. | 2026-02-08 |
| D-050 Backoff strategy | Exponential with jitter | base * 2^attempt + random jitter. Prevents thundering herd with thousands of concurrent agents. | 2026-02-08 |
| D-051 Fallback chain source | Config-driven with on-demand load_model() | Chain defined in config.toml. Adapters created on-demand at failure time. No wasted memory for unused providers. | 2026-02-08 |

## Learnings

(To be filled during implementation)

## Cross-References

- PRD: `PRD.md` (this directory)
- SDD: `SDD.md` (this directory)
- PLAN: `PLAN.md` (this directory)
- Step 6 Spec: `.claude/specs/006-provider-registry/`
- Registry: `src/arcllm/registry.py`
- Base Adapter: `src/arcllm/adapters/base.py`
- LLMProvider ABC: `src/arcllm/types.py`
- Config: `src/arcllm/config.py` + `src/arcllm/config.toml`
- Product PRD: `/Users/joshschultz/AI/arcllm/docs/arcllm-prd.md`
- Decision Log: `/Users/joshschultz/AI/arcllm/.claude/decision-log.md`
- Steering: `/Users/joshschultz/AI/arcllm/.claude/steering/`
