# Changelog

All notable changes to ArcRun will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
