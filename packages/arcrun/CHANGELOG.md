# Changelog

All notable changes to ArcRun will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0] - 2026-07-07

SPEC-043 SOTA loop controls: arcrun gains the six loop-control mechanisms as pure mechanism (the guards and persistence live in arcagent). Every wired seam deletes its dead predecessor in the same change — no two implementations.

### Added
- `checkpoint.py` — `LoopCheckpoint` + `to_checkpoint`/`apply_checkpoint`. The loop emits a serializable checkpoint at each turn boundary through an injected `RunState.on_checkpoint` hook (arcrun never persists). `run()`/`run_async()` gain `resume_from`: the registry is rebuilt fresh + frozen, the checkpoint's frozen tool-name set is verified (fail-closed on mismatch — a changed surface is a poisoned resume), and the loop re-enters at the saved turn without re-executing completed work (REQ-001..004).
- Unified circuit breaker — `check_breaker(state)` folds token/cost/turn caps and two new detectors into one top-of-turn hook + one terminator vocabulary: **runaway_loop** (identical tool-call signature repeated past `max_repeat`; a distinct-signature parallel batch counts as progress) and **error_cascade** (consecutive tool failures past `max_consecutive_errors`). `make_budget_breach_args` widened with both reasons (REQ-020..025).
- Wired `parallel_dispatch` — the react loop dispatches every turn's tool calls through `BatchClassifier` + `dispatch_batch` (read-only batches run concurrently, semaphore-bounded; state-modifying/unclassified run sequential, fail-closed). `Tool.classification` + `ToolRegistry.get_classification` feed the classifier (REQ-030..035).
- `PlanExecuteStrategy` (registered in `STRATEGIES`) — runs a flat list of independent ready items concurrently via the wired `ParallelDispatcher` and returns per-item outcomes, submission-order preserved, failures isolated. Never sees a DAG (REQ-050/051/055/056).
- Proactive HITL pause — `RunState.approval_provider` + `approval_required_tools`; before dispatching a flagged call the loop `await`s the provider (a grant proceeds, `None` fails closed). arcrun mints/verifies nothing — the predicate is a dumb membership test; tier policy is resolved by the caller (REQ-010..013).
- `run()`/`run_async()`/`run_stream()` thread `on_checkpoint`, `approval_provider`, `approval_required_tools`, `max_parallel`, `max_repeat`, `max_consecutive_errors`, `resume_from`.

### Removed
- The ad-hoc `parallel_safe` + raw `asyncio.gather` path in `react._execute_tool_calls` (replaced by the wired `dispatch_batch` — one dispatch path).
- The separate tail `max_turns` check (folded into `check_breaker`).
- The synthetic post-hoc word-split in `run_stream` that fabricated per-word `TokenEvent`s from already-complete content (SPEC-043 §3.5 streaming cut). `run_stream` now emits the real final content as one block; `TurnEndEvent`/`collect()`/`RunResult` are byte-for-byte unchanged. The real single-call `stream_llm_response` primitive is untouched.

## [0.8.0] - 2026-07-06

SPEC-038 sub-scope A: the per-run budget is now a real circuit-breaker. Token is the primary ceiling (present on both streaming and non-streaming paths); cost is the best-effort secondary.

### Added
- `RunState.max_tokens` — per-run token ceiling, enforced at the top of each turn beside the existing `max_cost_usd` check.
- `run()`/`run_async()` gain `max_tokens` / `max_cost_usd` parameters, threaded through `_build_state` onto `RunState` (the budget is now reachable via the public API).
- `make_budget_breach_args` supports a `max_tokens` reason; both breach sites (`max_turns`, budget) route through the single terminator (no inline payload dicts). A budget halt emits one `loop.completed` carrying the breached metric + observed tokens/cost.

### Removed
- Dead `RunState.token_budget` / `RunState.cost_budget` fields (zero readers; `cost_budget` duplicated `max_cost_usd`).

## [0.7.0] - 2026-07-06

SPEC-035 sub-scope C: workspace bind-mount for the execution backends + a shell entry point, so a confined agent shell keeps workspace access while the operator seed and WORM chains stay unreachable.

### Added
- `run_shell(command, *, tier, workspace, readonly_subpaths, caller_did, audit_sink, ...)` — routes a shell command through `resolve_execution_backend` (enterprise→container, federal→VM), emits `code_exec.backend.selected`, and fails closed (`IsolationUnavailableError`) at federal with no VM. Exported at the top level.
- `DockerBackend` / `VmBackend` now honor `workspace_mount` + `readonly_subpaths`: the workspace is bind-mounted `:rw` at `/workspace` (workdir), protected sub-paths are mounted `:ro`, and host `~/.arc`/`.audit` are never mounted. Implements the previously-declared-but-absent `supports_bind_mount`.

### Fixed
- `__version__` corrected (was stale at `0.5.0` while package metadata was ahead).

## [0.6.0] - 2026-07-05

SPEC-036: real tier-enforced code-execution sandbox, closing ASI05.

### Added

- **`VmBackend`** (`backends/vm.py`, `isolation="vm"`) — Hardware-isolated execution via Firecracker microVM. Launches through the jailer (namespaces, chroot/pivot_root, cgroups, privilege drop) with seccomp level 2 — never bare `firecracker`. Pluggable via the `VmEngine` Protocol; `gVisor`/`runsc` is a documented alternative engine (userspace-kernel isolation, not hardware-VM class — never an automatic fallback). Fails closed with `VmUnavailableError` when `/dev/kvm` is absent or the host isn't Linux.
- **`resolve_execution_backend(tier, relax, platform_supports_vm)`** (`builtins/execute.py`) — Pure, side-effect-free tier router: federal → `vm` (refuses if no KVM), enterprise → `docker` (container floor, cannot relax below it), personal → `docker` by default. `IsolationUnavailableError` and `IsolationRelaxationError` distinguish refusal reasons from an ordinary "none" outcome.
- **Personal sandbox-off** — A personal-tier operator may set `relax="off"` (aliases `"none"`/`"local"`) to run on `LocalBackend` (`isolation="none"`, full host access on their own machine). Every backend selection and every tier-permitted downgrade emits an audit event (`code_exec.backend.selected`, `code_exec.isolation.downgraded`) via `arctrust.audit.emit`.

### Changed

- **`execute_python` no longer runs bare host subprocesses by default** — `make_execute_tool()` now takes `tier`/`relax`/`caller_did`/`audit_sink`, resolves an isolation backend once at build time, and delegates every call to it through `SupportsSeparatedRun`. Tier and relax config are sourced by the caller (arccli); arcrun never reads config itself.

### Security

- Closes ASI05 (Unexpected Code Execution / RCE) with a real hardware-isolation floor at federal tier instead of a stripped-subprocess sandbox.
- Fail-closed isolation resolution: unavailable required isolation refuses execution rather than silently downgrading.
- Audit emitted on every backend selection and every downgrade, not just on failure.

## [0.5.0] - 2026-04-26

Major refactor: streaming runtime API, audit emission migrated to arctrust, spawn primitive moved up to arcagent (separation of concerns), and stricter manifest enforcement at all tiers.

### Added

- **Streaming runtime API** (`streams.py`) — `run_stream()` wraps `run()` and yields typed `StreamEvent` subclasses (`TokenEvent`, `ToolStartEvent`, `ToolEndEvent`, `TurnEndEvent`) for real-time output. Pure arcrun — no LLM-level streaming required; token text is derived from the final response and emitted progressively. `TurnEndEvent` always closes the stream.
- **Audit-event tests** — `tests/unit/backends/test_audit_events.py` and `tests/unit/test_streams_audit_events.py` lock down the events emitted by the new audit path.
- **Manifest-always-required test** — `tests/unit/backends/test_manifest_always_required.py` verifies signed-manifest enforcement across personal/enterprise/federal tiers (ADR-019).

### Changed

- **Audit emission migrated to arctrust** — All loop, tool, and backend audit events now emit through `arctrust.audit.emit(AuditEvent, sink)`. Single canonical schema; sinks fan out to JSONL, signed chain, and UI bridge. arcrun no longer constructs raw audit dicts.
- **Pairing/manifest signature required at every tier** — `UnsafeNoOp` skill verification bypass eliminated. Personal tier still verifies; only the keyset and stringency differ from federal.
- **Public API surface** — `streams` module added to the package export surface; legacy spawn entry points removed.
- **README rewritten** — Marketing prose replaced with a focused layer-position + public-surface reference.

### Removed

- **`arcrun.builtins.spawn`** — Spawn primitives moved to `arcagent.orchestration.spawn`. arcrun stays a pure loop; arcagent owns sub-run orchestration. Implements the concern split documented in `arcagent/orchestration/__init__.py` and the project CLAUDE.md.

### Security

- **Audit single-point-of-emission** — Aligned with ADR-019; arcrun no longer can drift from the canonical schema.
- **No-bypass verification** — Manifest signature is checked at every tier; `UnsafeNoOp` removed from the verification path.

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
