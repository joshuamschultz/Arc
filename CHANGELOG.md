# Changelog

All notable changes to the Arc monorepo will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
