# Changelog

All notable changes to ArcLLM will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-03-01

### Added

- **Azure OpenAI adapter** — Full Azure AI Foundry support including commercial (`.azure.com`) and government (`.azure.us`) endpoints. Azure-specific URL construction, `api-key` header auth, and `content_filter` finish reason handling.
- **Google Gemini adapter** — OpenAI-compatible adapter for Google's Gemini API with custom URL path handling.
- **Cohere adapter** — Provider adapter for Cohere's API.
- **xAI (Grok) adapter** — Provider adapter for xAI's Grok models.
- **Azure OpenAI provider TOML** — Full model catalog with pricing for GPT-4o, GPT-4o-mini, o1, o3-mini, and o4-mini across commercial and GCC-High deployments.
- **Google provider TOML** — Model catalog for Gemini 2.0 Flash, Gemini 1.5 Pro/Flash, and experimental models.
- **xAI provider TOML** — Model catalog for Grok-2, Grok-2 Vision, and Grok-3 models.
- **Cohere provider TOML** — Model catalog for Command R/R+ models.
- **Rate-limit-aware retry** — `rate_limit_max_retries` config (default: 6) gives 429 responses a higher retry budget. `Retry-After` header honored without capping since rate limits are guaranteed to resolve.
- **Azure OpenAI test suite** — Unit tests covering URL construction, api-key auth, content filter handling, and GCC-High endpoints.
- **QueueModule** — Bounded concurrency with backpressure for LLM calls. Gates `invoke()` through `asyncio.BoundedSemaphore` with configurable `max_concurrent` (default: 2), `call_timeout` (default: 60s), and `max_queued` (default: 10). Excess callers rejected with `QueueFullError`. Send-time-only timeouts (clock starts after semaphore acquired). OTel span attributes for wait time and queue depth.
- **CircuitBreakerModule** — Per-provider circuit breaker with CLOSED → OPEN → HALF_OPEN state machine. Configurable `failure_threshold` (default: 5), `cooldown_seconds` (default: 30), `half_open_max_calls` (default: 1). Thread-safe state transitions. Emits `circuit_change` TraceRecords on state transitions. Queryable state via `get_state()` for REST APIs.
- **TraceStore** — Append-only, hash-chained (SHA-256) LLM call recording. `TraceRecord` frozen Pydantic model captures provider, model, timing, tokens, cost, request/response bodies, and phase sub-timings. `JSONLTraceStore` implementation with daily file rotation, cursor-based pagination, and `verify_chain()` for tamper-evident audit. Uses RFC 8785 (JCS) canonical JSON for deterministic hashing.
- **ConfigController** — Runtime get/set for LLM configuration with atomic swap. Immutable `ConfigSnapshot` (frozen Pydantic model). Patchable keys: model, temperature, max_tokens, budget limits, failover chain. `on_change` callbacks and `TraceRecord` audit emission on every mutation.
- **Queue error types** — `QueueFullError` and `QueueTimeoutError` exception classes with structured attributes for caller-side decision making.
- **Telemetry TraceRecord integration** — `on_event` callback and `trace_store` threading through `load_model()` into TelemetryModule. `agent_label` for multi-agent identification. `store_raw_bodies` toggle. `get_budget_state()` API for REST queries.

### Changed

- **Provider model catalogs updated** — Anthropic (Claude 4.6/4.5/3.5 generations with current pricing), OpenAI (GPT-4o, o1, o3/o4-mini), Mistral (Large, Small, Codestral, Pixtral), Groq (Llama 3.3/3.2, DeepSeek-R1), Fireworks (Llama 3.3/3.2, DeepSeek), HuggingFace, Ollama, Together, DeepSeek — all refreshed with latest models and pricing.
- **Anthropic default model** — Changed from `claude-sonnet-4-20250514` to `claude-sonnet-4-6`.
- **Ruff lint config** — Added comprehensive rule selection (`E`, `W`, `F`, `I`, `N`, `UP`, `B`, `A`, `S`, `T20`, `RUF`) and per-file ignores for tests and walkthroughs.
- **Code formatting** — Applied consistent formatting across registry, retry, routing, and telemetry modules.
- **Module stack order** — Updated to: Otel → Queue → Telemetry → CircuitBreaker → Audit → Security → Retry → Fallback → RateLimit → [Router|Adapter].
- **`load_model()` API** — New parameters: `on_event`, `trace_store`, `agent_label`, `circuit_breaker`, `queue`. All optional, threaded through to the appropriate modules.
- **Config TOML** — Added `[modules.queue]` section with defaults.

## [0.2.0] - 2026-02-21

### Added

- **Budget enforcement** — Per-scope spend tracking with calendar period resets (monthly/daily). Pre-flight cost estimation blocks calls that would exceed per-call limits. Post-call deduction tracks actuals. Configurable enforcement modes: `block`, `warn`, or `log`.
- **Budget accumulator** — Thread-safe `BudgetAccumulator` with double-check locking for PEP 703 (free-threading) readiness. Automatic UTC calendar period resets. Negative cost injection prevention via clamping.
- **Classification-aware routing** — `RoutingModule` routes LLM calls to specific providers/models based on data classification level (e.g., CUI to Anthropic, unclassified to OpenAI). Configurable enforcement modes.
- **Budget error type** — `ArcLLMBudgetError` exception with scope, limit type, and dollar amounts for caller-side decision making (retry later, switch model, alert).
- **Budget security tests** — Adversarial tests for negative cost injection, scope Unicode homoglyph attacks, concurrent budget manipulation, and calendar boundary race conditions.
- **Routing security tests** — Tests for classification bypass attempts, Unicode normalization attacks, and concurrent routing manipulation.
- **Budget telemetry integration** — Budget fields merged into telemetry module config. No separate `[modules.budget]` section — budget is a telemetry concern.
- **Routing stack tests** — Integration tests verifying routing + telemetry + budget modules work together in the module chain.

### Changed

- **Config schema** — Merged budget configuration into `[modules.telemetry]` section. Removed standalone `[modules.budget]` section. Budget fields are optional — budget is disabled when none are present.
- **Telemetry module** — Extended with budget pre-check (before LLM call) and post-deduct (after response) in the `invoke()` flow. Added OpenTelemetry span attributes for budget metrics.
- **Registry** — Enhanced `load_model()` to support routing module integration with provider/model override at call time.

### Security

- Budget scope validation with NFKC Unicode normalization and strict regex (`^[a-z][a-z0-9_:.\-]{0,127}$`) prevents homoglyph and injection attacks.
- Cost values clamped to `max(0.0, cost)` to prevent negative cost injection that could artificially inflate remaining budget.
- Thread-safe accumulator design ready for Python free-threading (PEP 703).

## [0.1.0] - 2026-02-01

### Added

- Initial release with core foundation.
- 11 LLM provider adapters (Anthropic, OpenAI, DeepSeek, Mistral, Groq, Together, Fireworks, HuggingFace, HuggingFace TGI, Ollama, vLLM).
- Opt-in module system: retry, fallback, rate limiting, telemetry, audit, security, OpenTelemetry.
- PII redaction, HMAC request signing, vault-based key resolution.
- Config-driven provider management via TOML files.
- Pydantic v2 type system for all data boundaries.
