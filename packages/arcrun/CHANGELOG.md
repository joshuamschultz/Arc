# Changelog

All notable changes to ArcRun will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-04-18

SPEC-017 loop hardening: parallel tool dispatch, structured task completion, budget caps.

### Added

- **Parallel tool dispatch** (`parallel_dispatch.py`) — `BatchClassifier` partitions tool batches by classification (read-only vs state-modifying) with an implicit-dependency heuristic (shared path args force sequential). `ParallelDispatcher` runs read-only batches via `asyncio.gather(return_exceptions=True)` bounded by `asyncio.Semaphore` (default 10, FIPS mode 4). `SequentialDispatcher` fallback for mixed batches. `dispatch_batch()` is the top-level entry point. Submission-order result preservation regardless of completion order. Optional monotonic sequence numbers for audit ordering.
- **`task_complete` builtin** (`builtins/task_complete.py`) — Structured loop-termination signal. `TaskCompleteArgs` Pydantic model with `status` (`success|partial|failed`), `summary`, optional `artifacts`/`next_steps`/`error`. `make_task_complete_tool()` for registration, `make_budget_breach_args()` helper for programmatic budget-breach payloads.
- **Loop termination integration** (`strategies/react.py`) — React strategy detects `task_complete` calls per turn, emits `loop.completed` event with the payload, and terminates cleanly.
- **Budget caps** — `RunState` gains `max_cost_usd` and `completion_payload`. React strategy enforces `max_turns` (synthesized `failed` completion) and `max_cost_usd` (checked before each turn) per SPEC-017 R-032.

### Changed

- **Strategy prompt provider** — Strategies now expose `prompt_guidance` (abstract property on `Strategy` ABC) describing when and how the model should leverage each execution strategy. New `get_strategy_prompts()` public API assembles guidance sections for spawning, code execution, and strategy selection based on active tools and allowed strategies. Prompts flow UP to the agent layer without breaking separation of concerns.
- **Public API** — `get_strategy_prompts()` and `parallel_dispatch` module added to the `arcrun` export surface.
- **`RunState`** — New optional fields `completion_payload`, `max_cost_usd`. Backward-compatible defaults so existing callers are unaffected.

### Fixed

- **`TestThreadSafeEventBus.test_lock_exists`** — Works on Python 3.13 where `threading.Lock` became a factory rather than a type.

## [0.3.0] - 2026-03-01

### Changed

- **Code formatting & lint compliance** — Applied consistent formatting across all source files: multi-line function signatures, explicit `strict=False` on `zip()` calls, expanded `bus.emit()` calls for readability. Zero ruff violations.
- **Ruff lint config** — Added comprehensive rule selection (`E`, `W`, `F`, `I`, `N`, `UP`, `B`, `A`, `S`, `T20`, `RUF`) and per-file ignores for tests, sandbox builtins, and walkthroughs.
- **Import modernization** — Replaced `typing.Callable` with `collections.abc.Callable` per PEP 585 (`UP` rules).
- **Exception type update** — `asyncio.TimeoutError` replaced with builtin `TimeoutError` per Python 3.11+ deprecation.
- **Spawn string formatting** — Simplified f-string concatenation in child system prompt construction.

## [0.2.0] - 2026-02-21

### Added

- **Tamper-evident event chain** — SHA-256 hash chain on all events. Each event contains `sequence`, `prev_hash`, and `event_hash` fields. Genesis event uses `"0" * 64` as prev_hash. `verify_chain()` validates integrity of the full audit trail.
- **Immutable events** — `Event` dataclass is now `frozen=True` with `MappingProxyType` data field. Events cannot be mutated after creation.
- **Chain verification API** — `verify_chain(events)` returns `ChainVerificationResult` with `valid`, `verified_count`, `first_invalid_index`, and `error` fields.
- **Container sandbox** — `make_contained_execute_tool()` factory creates a Docker-isolated Python execution tool. Runs agent-generated code in ephemeral containers with configurable memory limits, CPU quotas, network isolation, and read-only filesystems.
- **Sandbox error hierarchy** — `SandboxError` base with `SandboxTimeoutError`, `SandboxOOMError`, `SandboxRuntimeError`, and `SandboxUnavailableError` subtypes for precise error handling.
- **Adversarial security test suite** — 36 tests across 8 categories covering OWASP LLM Top 10 and OWASP Agentic AI Top 10 attack vectors:
  - Prompt injection (LLM01, ASI01)
  - Path traversal (ASI05)
  - Steering injection (ASI01, ASI06)
  - Tool injection (ASI02, ASI04)
  - Resource exhaustion (LLM10, ASI08)
  - Spawn depth bomb (ASI08)
  - Event tampering (AU-9, AU-10)
  - Timing attacks (AU-8)
- **Security documentation** — Threat model, NIST 800-53 control mapping, and adversarial test catalog in `docs/security/`.
- **Spawn E2E tests** — End-to-end tests for recursive task decomposition with spawn tool.
- **Container execute tests** — Unit tests for Docker sandbox including timeout, OOM, and network isolation scenarios.

### Changed

- **Event dataclass** — Now `frozen=True` (immutable). `data` field changed from `dict` to `MappingProxyType` for deep immutability.
- **EventBus** — Maintains hash chain state (`_sequence`, `_prev_hash`). Thread-safe via `threading.Lock`. Emits events with computed hashes.
- **Types** — `LoopResult.events` now contains hash-chained events. Added `SandboxConfig` fields for container mode.
- **Builtins __init__** — Re-exports sandbox error types and contained execute factory.

### Security

- Hash chain provides tamper-evident audit trail meeting NIST 800-53 AU-9 (Protection of Audit Information) and AU-10 (Non-Repudiation).
- Container sandbox isolates agent-generated code execution (OWASP ASI05 — Unexpected Code Execution / RCE).
- Event immutability prevents post-emission tampering of audit records.
- Adversarial test suite validates resilience against 8 attack categories.

## [0.1.0] - 2026-02-01

### Added

- Initial release with core execution loop.
- ReAct and CodeExec execution strategies.
- Deny-by-default sandbox with tool allowlists and custom checkers.
- Event system with typed events for full audit trails.
- Dynamic tool registry with mid-execution tool management.
- Steering and follow-up for mid-execution intervention.
- Context transform hook for context window management.
- Sandboxed Python execution via `make_execute_tool()`.
- Spawn tool for recursive task decomposition.
