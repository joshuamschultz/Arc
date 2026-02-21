# Changelog

All notable changes to ArcLLM will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
