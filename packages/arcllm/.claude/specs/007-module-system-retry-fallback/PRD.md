# PRD: Module System + Retry + Fallback

> Feature-specific requirements for ArcLLM Step 7.
> References steering docs in `.claude/steering/`.

---

## Feature Overview

### Problem Statement

ArcLLM adapters currently have no resilience — if an API call fails (rate limit, server error, provider outage), the error propagates directly to the agent. Agents must implement their own retry logic and provider switching, leading to duplicated boilerplate across thousands of agents. There is also no module system — no standardized way to layer cross-cutting concerns (retry, telemetry, audit, rate limiting) onto the invoke() path without modifying adapter code.

### Goal

1. **Establish the module system pattern** — a `BaseModule` wrapper class that implements `LLMProvider`, wraps an inner provider, and intercepts `invoke()` calls. This becomes the foundation for all future modules.

2. **Implement RetryModule** — retries transient failures (429, 500, 502, 503, 529, connection errors) with exponential backoff + jitter.

3. **Implement FallbackModule** — on failure, walks a config-driven provider chain using `load_model()` to create fallback adapters on-demand.

4. **Integrate with registry** — `load_model()` reads module config from `config.toml` and automatically stacks enabled modules around the adapter.

### Success Criteria

- `BaseModule` is a wrapper class implementing `LLMProvider` with `invoke()` delegation
- `RetryModule` retries on 429/500/502/503/529 with exponential backoff + jitter
- `RetryModule` respects `max_retries`, `backoff_base_seconds`, `max_wait_seconds` config
- `RetryModule` raises the original exception after max retries exhausted
- `FallbackModule` catches exceptions and tries next provider in config chain
- `FallbackModule` uses `load_model()` to create fallback adapters on-demand
- `FallbackModule` raises the last exception if all providers in chain fail
- Modules can be stacked: `RetryModule(FallbackModule(adapter))`
- `load_model("anthropic", retry=True)` enables retry module
- `load_model("anthropic")` with `[modules.retry] enabled = true` in config also enables it
- Load-time kwargs override config.toml settings
- Agent code is unchanged — still just `model.invoke(messages, tools)`
- All existing 149 tests pass unchanged
- New module tests are fully mocked (no real API calls)
- Zero new dependencies

---

## Requirements

### Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|------------|----------|------------|
| FR-1 | `BaseModule` class wraps any `LLMProvider` with delegated `invoke()` | P0 | Implements LLMProvider, holds `inner` reference |
| FR-2 | `RetryModule` retries on HTTP status 429, 500, 502, 503, 529 | P0 | Configurable retry codes |
| FR-3 | `RetryModule` retries on `httpx.ConnectError`, `httpx.TimeoutException` | P0 | Connection-level failures |
| FR-4 | `RetryModule` uses exponential backoff: `base * 2^attempt` | P0 | Configurable base and max wait |
| FR-5 | `RetryModule` adds random jitter to backoff | P0 | Prevents thundering herd |
| FR-6 | `RetryModule` raises original exception after `max_retries` exhausted | P0 | No silent swallowing |
| FR-7 | `RetryModule` passes through non-retryable errors immediately | P0 | 400, 401, 403 not retried |
| FR-8 | `FallbackModule` catches exceptions and tries next provider in chain | P0 | Config-driven chain list |
| FR-9 | `FallbackModule` creates fallback adapters via `load_model()` on-demand | P0 | No pre-loaded adapters |
| FR-10 | `FallbackModule` raises last exception if entire chain exhausted | P0 | Clear error with chain info |
| FR-11 | Registry integration — enabled modules stacked around adapter | P0 | Config or load-time activation |
| FR-12 | Load-time kwargs override config.toml module settings | P0 | `retry={"max_retries": 5}` wins |
| FR-13 | `BaseModule` exposes `name` and `validate_config()` via delegation | P1 | Transparent wrapper |
| FR-14 | Module stacking order: outermost runs first | P1 | Retry wraps Fallback wraps adapter |
| FR-15 | `RetryModule` respects `Retry-After` header when present | P2 | Anthropic sends this on 429 |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|------------|-----------|
| NFR-1 | Module wrapping overhead | <0.1ms per invoke() delegation |
| NFR-2 | Zero new dependencies | Uses stdlib `random`, `asyncio` |
| NFR-3 | All tests run without real API calls | Fully mocked |
| NFR-4 | Existing 149 tests unaffected | Zero regressions |
| NFR-5 | Module code is independently testable | No adapter setup needed |

---

## User Stories

### Agent Developer

> As an agent developer, I want `load_model("anthropic", retry=True)` to give me a model that automatically retries transient failures, so I don't need to implement retry logic in every agent.

### Platform Engineer

> As a platform engineer, I want to define a fallback chain (`["anthropic", "openai"]`) in config.toml so that if Anthropic goes down, all agents automatically fail over to OpenAI without code changes.

### Module Developer (Future)

> As a module developer, I want a clear `BaseModule` pattern to follow when building new modules (telemetry, audit, budget), so each module is a small, focused file with a consistent interface.

---

## Out of Scope (Step 7)

- Rate limiting (Step 8)
- Routing (Step 9)
- Telemetry/audit/budget (Steps 10-12)
- Sync wrapper for modules
- Circuit breaker pattern (could be added later as enhancement)
- Per-model retry config (all models on a provider share retry settings)

---

## Personas Referenced

- **Agent Developer** (primary) — see `steering/product.md`
- **Platform Engineer** (secondary) — see `steering/product.md`

---

## Dependencies

| Dependency | Type | Status |
|------------|------|--------|
| Step 1-6 (Core Foundation) | Prerequisite | COMPLETE |
| `LLMProvider` ABC | Interface | Defined in types.py |
| `load_model()` registry | Integration point | Implemented in registry.py |
| `config.toml` module toggles | Config | Already has [modules.retry] and [modules.fallback] |
| `ArcLLMAPIError` | Exception type | Defined in exceptions.py |
| httpx exceptions | Error detection | httpx already in deps |
