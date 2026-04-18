# Changelog

All notable changes to the Arc monorepo will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2026-04-18] — SPEC-017 Arc Core Hardening

Federal-first hardening pass across the Arc monolith. Production-grade tool policy pipeline, dynamic tool surface with layered defense, unified proactive scheduling engine, Prometheus metrics, and tier-aware self-modification. Ships as:

- `arc-agent` 0.2.0 → 0.3.0
- `arcrun` 0.3.0 → 0.4.0
- `arccmd` 0.2.0 → 0.3.0

See each package CHANGELOG for the detailed per-package breakdown. Highlights:

### Added

- **Tool Policy Pipeline** (arcagent) — 5-layer first-DENY-wins, fail-closed evaluator with LRU cache (p95 < 1ms). Tier-aware composition: Federal=5, Enterprise=4, Personal=1. Shadow mode + restricted mode for air-gapped / stale-bundle situations.
- **Dynamic Tool Surface** (arcagent) — `@tool` decorator, `DynamicToolLoader`, AST validator rejecting 9 CVE-cited bypass categories, scrubbed `RESTRICTED_BUILTINS`, deny-by-default origin-allowlisted egress proxy.
- **Self-modification tools** (arcagent) — `create_skill`, `improve_skill`, `create_tool`, `create_extension`, `list_artifacts`, `reload_artifacts`. Tier gates: federal denies dynamic code (NIST 800-53 SI-7(15), CM-5, CM-8); enterprise approval; personal allowed.
- **Unified Proactive Engine** (arcagent) — Replaces `pulse` + `scheduler` modules. Min-heap timer, drift-free reschedule, heartbeat isolation, per-schedule circuit breaker, timezone + DST handling, `LeaderElection` Protocol.
- **Parallel tool dispatch** (arcrun) — Read-only batches execute concurrently via `asyncio.gather` bounded by semaphore; state-modifying or implicit-dep-colliding batches run sequential. Submission-order results preserved.
- **`task_complete` builtin** (arcrun) — Structured loop-termination signal. Budget caps (`max_turns`, `max_cost_usd`) enforced with automatic `failed` completion on breach.
- **Prometheus metrics** (arcagent) — In-process `MetricRegistry` with counters/gauges/histograms + text exposition + audit-sink adapters. Zero external deps.
- **Capability-composition safety** — `ForbiddenCompositionChecker` rejects batches whose combined capability tags match a forbidden set (e.g. `file_read + network_egress = exfiltration`).
- **CLI mirror** (arccli) — `arc agent policy`, `arc agent completion`, `arc agent schedule` subcommands for scriptable access.
- **Adversarial security suite** — 42 tests covering import bypass, frame traversal, dynamic exec, `sys.modules` access, codec attacks, `__builtins__` mutation, starred unpacking, capability composition.
- **Runbook** — `packages/arcagent/docs/runbooks/spec-017-operations.md`.

### Removed

- **Legacy `modules/pulse/` and `modules/scheduler/` modules** (arcagent) — Functionality migrated to `modules/proactive/`. Per SPEC-017 R-040: no compat shim.

### Fixed

- **ArcLLM `on_event` bridge wiring** — `create_arcllm_bridge()` now actually runs; ArcLLM events (`llm_call`, `config_change`, `circuit_change`) reach the Module Bus.
- **`ArcAgent.shutdown()`** — Closes the httpx client so connection pools release deterministically.
- **Module loader** — Checks `enabled` before validating `entry_point` (lets descriptor-only `MODULE.yaml` files coexist without breaking startup).
- **Messaging `ack` byte_pos** — Stores the real stream end offset in cursor instead of the prior `byte_pos=0` that forced full rescans.

### Security

- **NIST 800-53 AU-2** — Every policy evaluation, self-mod action, schedule tick, and completion event audit-logged with agent DID + rule ID.
- **NIST 800-53 SI-7(15), CM-5, CM-8** — Federal tier refuses dynamic tool/extension creation at the tool level, BEFORE the loader is consulted.
- **OWASP LLM02 / ASI02 / ASI05** — Policy pipeline on every tool call, AST validator (CVE-2023-37271 generator-frame bypass, CVE-2025-68668 ctypes FFI bypass, etc.), deny-by-default egress proxy.

---

## [Pre-2026-04-18] — prior Unreleased

Multi-agent observability platform, vault-backed secrets, strategy prompt provider, messaging integrations, and continued security hardening.

---

### ArcAgent

#### Added
- Vault-backed secret resolution for extension API (`api.get_secret()`).
- Strategy prompt provider integration — ArcRun guidance merges into agent system prompt.
- UI reporter module for real-time agent observability via ArcUI WebSocket.
- Messaging module with unified Slack and Telegram integrations.
- Slack module with bidirectional bot and setup runbook.
- Telegram module with bot integration.
- Skill improver module for autonomous skill evolution.
- Pulse module with per-check circuit breakers for health monitoring.
- Bio memory enhancements — daily notes, entity helpers, facts, deep consolidator. All entity mutations now batch-promoted to team shared knowledge.

#### Fixed
- DID persistence across agent restarts — identity survives stop/start cycles.
- Azure Key Vault backend accepts `cache_ttl_seconds` in constructor.
- Pulse engine prompt reworded to avoid Azure content filter jailbreak detection.
- Slack error handling improvements for 400/content filter/rate limit responses.

#### Security
- DID files written with `0o600` permissions.

---

### ArcLLM

#### Changed
- OpenAI adapter auto-converts `system` → `developer` role for o-series reasoning models.

#### Fixed
- Timeout configuration in dependency specification.

#### Security
- Trace store file permissions hardened to `0o600`/`0o700` (NIST AU-9).
- Hash chain tamper detection on startup — verifies last 10 records (NIST AU-10).
- Provider name input validation prevents module injection (ASI-04, NIST SI-10).

---

### ArcRun

#### Added
- Strategy prompt provider — strategies expose `prompt_guidance` and `get_strategy_prompts()` public API for model-facing guidance.

---

### ArcUI

#### Added
- Historical trace loading on page refresh from JSONL trace store.
- Real timeseries chart data via `/api/stats/timeseries`.
- Tool call display with arguments in trace detail panel.
- Single trace JSON export.
- Multi-agent WebSocket transport, agent registry, and subscription manager.
- Agent routes for listing, detail, and status queries.
- Event buffer with overflow policy for bursty agent traffic.
- Authentication middleware for API and WebSocket connections.
- ArcLLM config routes for runtime inspection and mutation.

#### Changed
- Server architecture refactored from single-agent trace viewer to multi-agent observability platform.

#### Security
- API input validation on all endpoints (trace ID, cursor, filters, window, format).
- Audit logging on all API requests and WebSocket connections.

#### Fixed
- WebSocket connection status stuck on "Connecting".
- Pulse transport event type handling.

---

### ArcCLI

#### Added
- `arc agent ui` command for launching ArcUI dashboard.
- Telegram setup wizard.

#### Changed
- PyPI package renamed from `arccli` to `arccmd` (name collision).

---

### ArcTeam

#### Changed
- File store updates and public API export refinements.

---

### Monorepo

#### Changed
- Version alignment: pyproject.toml, `__version__`, and changelogs now consistent across all packages.
- `__version__` added to arcllm and arcrun `__init__.py`.
- Python minimum version dropped from 3.12 to 3.11 across all packages.

---

## [0.3.0] - 2026-03-01

New LLM provider adapters, model catalog refresh, rate-limit-aware retry, code quality hardening, and PyPI publishing infrastructure.

---

### ArcLLM `0.3.0`

#### Added
- **4 new provider adapters** — Azure OpenAI (commercial + GCC-High), Google Gemini, Cohere, and xAI (Grok). Total adapters: 15.
- **QueueModule** — Bounded concurrency with backpressure (`max_concurrent`, `call_timeout`, `max_queued`). Send-time-only timeouts with OTel instrumentation.
- **CircuitBreakerModule** — Per-provider CLOSED/OPEN/HALF_OPEN state machine with configurable thresholds, cooldown, and event emission.
- **TraceStore** — Append-only, SHA-256 hash-chained LLM call recording with `JSONLTraceStore` (daily rotation, cursor pagination, chain verification). RFC 8785 canonical JSON hashing.
- **ConfigController** — Runtime config get/set with atomic swap, immutable snapshots, change callbacks, and audit trail.
- **Rate-limit-aware retry** — Dedicated `rate_limit_max_retries` (default: 6) for 429 responses with `Retry-After` header support.
- **Provider TOML catalogs** — Azure OpenAI, Google, Cohere, xAI with full model specs and pricing.
- **Queue error types** — `QueueFullError`, `QueueTimeoutError` for structured error handling.

#### Changed
- All provider model catalogs updated to latest models and pricing (Anthropic Claude 4.6, OpenAI GPT-4o/o-series, Mistral, Groq, etc.).
- Anthropic default model updated to `claude-sonnet-4-6`.
- Module stack order updated: Otel → Queue → Telemetry → CircuitBreaker → Audit → Security → Retry → Fallback → RateLimit.
- `load_model()` API expanded with `on_event`, `trace_store`, `agent_label`, `circuit_breaker`, `queue` parameters.
- Comprehensive ruff lint configuration added.

---

### ArcRun `0.3.0`

#### Changed
- Code formatting and lint compliance across all source files.
- Import modernization: `typing.Callable` → `collections.abc.Callable` (PEP 585).
- `asyncio.TimeoutError` → builtin `TimeoutError` (Python 3.11+).
- Comprehensive ruff lint configuration added.

---

### Monorepo

#### Added
- **ArcPrompt package** — Placeholder package with PyPI publish workflow.
- **PyPI publishing infrastructure** — GitHub Actions workflows for all packages.

#### Changed
- Root ruff config expanded with per-file ignores for tests and walkthroughs.
- README updated with expanded project overview.
- Workspace config updated to include arcprompt.

---

## [0.2.0] - 2026-02-21

Security hardening, budget enforcement, tamper-evident audit trails, biological memory, team knowledge management, and CLI initialization across the full stack.

---

### ArcLLM `0.2.0`

#### Added
- **Budget enforcement** — Per-scope spend tracking with calendar period resets (monthly/daily). Pre-flight cost estimation, post-call deduction, and configurable enforcement modes (`block`, `warn`, `log`).
- **Classification-aware routing** — `RoutingModule` routes LLM calls to providers/models based on data classification level. CUI to cleared providers, unclassified to cost-optimized providers.
- **Budget error type** — `ArcLLMBudgetError` with scope, limit type, and dollar amounts for caller-side decision making.
- **Security test suite** — Adversarial tests for budget manipulation (negative cost injection, Unicode homoglyph attacks, concurrent manipulation) and routing bypass attempts.

#### Changed
- Budget config merged into `[modules.telemetry]`. Removed standalone `[modules.budget]` section.
- Telemetry module extended with pre-check/post-deduct budget flow and OpenTelemetry span attributes.

#### Security
- Budget scope validation with NFKC normalization prevents homoglyph attacks.
- Cost clamping to `max(0.0, cost)` prevents negative cost injection.
- Thread-safe accumulator design for PEP 703 free-threading readiness.

---

### ArcRun `0.2.0`

#### Added
- **Tamper-evident event chain** — SHA-256 hash chain on all events with `verify_chain()` API. Meets NIST 800-53 AU-9/AU-10.
- **Immutable events** — `Event` is now `frozen=True` with `MappingProxyType` data. No post-emission tampering.
- **Container sandbox** — Docker-isolated Python execution via `make_contained_execute_tool()`. Memory limits, CPU quotas, network isolation, read-only filesystem.
- **Sandbox error hierarchy** — `SandboxError` base with `TimeoutError`, `OOMError`, `RuntimeError`, `UnavailableError` subtypes.
- **Adversarial test suite** — 36 tests across 8 attack categories (prompt injection, path traversal, steering injection, tool injection, resource exhaustion, spawn depth bomb, event tampering, timing attacks).
- **Security documentation** — Threat model, NIST 800-53 mapping, and adversarial test catalog.

#### Changed
- Events now carry `sequence`, `prev_hash`, and `event_hash` fields for chain integrity.
- `EventBus` maintains thread-safe hash chain state.

---

### ArcAgent `0.2.0`

#### Added
- **Biological memory module** — Long-term identity-aware memory with identity manager, working memory, consolidator, and retriever. Tracks agent personality, episodic experiences, and session-scoped reasoning.
- **Shared text sanitizer** — Centralized ASI-06 defense with NFKC normalization, zero-width character stripping, and control character removal.
- **Bio memory CLI** — `arc agent bio_memory status|identity|episodes|working`.
- **Integration and unit tests** — Full test coverage for biological memory lifecycle and retrieval accuracy.

#### Changed
- Entity extractor refactored to use shared sanitizer (DRY).

---

### ArcCLI `0.2.0`

#### Added
- **`arc init`** — Unified initialization wizard with tier-based presets (`open`, `enterprise`, `federal`). Generates configs, validates API keys.
- **`arc llm init`** — ArcLLM-specific setup with provider config generation.
- **`arc team init`** — Team data directory setup with HMAC key generation.
- **`arc team status`** — Team overview (entities, channels, messages, audit entries).
- **`arc team config`** — Team configuration display.
- **`arc team memory`** — Full team memory management (status, entities, entity, search, rebuild-index, config).
- **Bio memory CLI** — `arc agent bio_memory` module group.

---

### ArcTeam `0.2.0`

#### Added
- **Team memory subsystem** — Institutional knowledge management with:
  - Entity storage with markdown frontmatter
  - BM25 search with wiki-link graph traversal
  - Index manager with dirty-state tracking
  - Promotion gate for agent-to-team knowledge transfer
  - Data classification types (CUI/FOUO/Unclassified)
  - Standalone `arc-memory` CLI

#### Changed
- Added `python-frontmatter` and `rank-bm25` dependencies.
- Added `arc-memory` entry point.

---

## [0.1.0] - 2026-02-01

Initial release of the Arc monorepo.

### ArcLLM `0.1.0`
- 11 LLM provider adapters with direct HTTP (no SDKs).
- Opt-in module system: retry, fallback, rate limiting, telemetry, audit, security, OpenTelemetry.
- PII redaction, HMAC request signing, vault integration.
- Config-driven provider management via TOML.

### ArcRun `0.1.0`
- Core execution loop with ReAct and CodeExec strategies.
- Deny-by-default sandbox with tool allowlists.
- Full event audit trail on every action.
- Dynamic tool registry, steering, context transforms.

### ArcAgent `0.1.0`
- Ed25519 cryptographic identity with W3C DID format.
- Token-budgeted context manager with tiered compaction.
- Tool registry with 4-transport architecture.
- Event-driven module bus, session persistence, skill discovery.
- Memory module with hybrid search and entity extraction.

### ArcCLI `0.1.0`
- Unified `arc` CLI for LLM, agent, run, team, extension, and skill management.
- Global `--json` output support.

### ArcTeam `0.1.0`
- Four collaboration primitives: messaging, tasks, knowledge base, file store.
- Universal addressing via typed URIs.
- Append-only audit trail (NIST 800-53 compliant).
- `StorageBackend` protocol with file and memory backends.
