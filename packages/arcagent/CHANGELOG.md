# Changelog

All notable changes to ArcAgent (`arc-agent` on PyPI) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-04-18

Federal-first hardening: tool policy pipeline, dynamic tool surface with layered defense, unified proactive engine, Prometheus metrics, tier-aware self-modification. Implements SPEC-017.

### Added

- **Tool Policy Pipeline** (`core/tool_policy.py`) — 5-layer first-DENY-wins, fail-closed evaluator with LRU cache (p95 < 1ms @ 100 rules). Layers: Global → Provider → Agent → Team → Sandbox. Tier-aware `build_pipeline()` factory emits the correct stack per deployment (Federal=5, Enterprise=4, Personal=1). Shadow mode for safe rollout. Restricted mode when policy bundle stale.
- **Dynamic tool surface** (`tools/_decorator.py`, `tools/_dynamic_loader.py`, `tools/_egress.py`) — `@tool` decorator with type-hint schema inference; `DynamicToolLoader` pipeline: encoding check → 9-category AST validation → `RESTRICTED_BUILTINS` sandbox compile → registration. Origin-allowlisted egress proxy for dynamic tool network access.
- **Self-modification tools** (`tools/skill_tools.py`, `tools/tool_tools.py`, `tools/extension_tools.py`) — `create_skill`, `improve_skill`, `create_tool`, `create_extension`, `list_artifacts`, `reload_artifacts`. Tier gates: federal denies dynamic code; enterprise requires approval (audit-logged); personal allows. Every action emits a structured audit event.
- **Unified Proactive Engine** (`modules/proactive/`) — Replaces the legacy `pulse` + `scheduler` modules. Single asyncio task, min-heap priority queue, drift-free rescheduling (`last_actual_run + interval - overhead`), clock-warp detection, wake idempotency, heartbeat isolation (dedicated `HeartbeatContext` — no session state leak). `CircuitBreaker` (Resilience4j pattern) + `LeaderElection` Protocol with `NoOpLeaderElection` / `InMemoryElection` implementations. Timezone helper handles IANA zones + DST + overnight windows.
- **Prometheus metrics** (`core/metrics.py`) — In-process `MetricRegistry` with counters/gauges/histograms, text exposition format, and audit-sink adapters for policy and proactive events. Ships without `prometheus_client` dependency.
- **Capability-composition safety** — `ForbiddenCompositionChecker` rejects batches whose combined capability tags match a forbidden set (e.g. `file_read + network_egress = exfiltration`). Addresses non-compositional safety per arXiv:2603.15973.
- **`classification` on `RegisteredTool`** — Every tool declares `read_only` or `state_modifying`. All 7 built-ins annotated: `read/grep/find/ls` = `read_only`; `bash/edit/write` = `state_modifying`. Plus `capability_tags` for composition checks.
- **Adversarial test suite** — 42 tests under `tests/security/` covering AST bypass categories (CVE-cited), restricted-builtin enforcement, egress deny, capability composition. Designed to gate CI.
- **Runbook** — `docs/runbooks/spec-017-operations.md` — policy ops, scheduling, tier config, metrics wiring, incident response, legacy module migration.

### Changed

- **`ToolRegistry` dispatch** — When constructed with a `ToolPolicyPipeline`, every tool call flows through first-DENY-wins evaluation before reaching the tool's `execute()`. No sudo path. Pipeline is opt-in to preserve backward compatibility with existing deployments.
- **`ArcAgent._ensure_model`** — Wires `create_arcllm_bridge()` via the new `on_event` parameter on `load_eval_model()` so ArcLLM events (`llm_call`, `config_change`, `circuit_change`) now reach the Module Bus. Closes a long-standing integration gap.
- **`ArcAgent.shutdown`** — Closes the `httpx` client owned by the LLM model so connection pools are released deterministically.
- **Module loader** — Checks `enabled` BEFORE validating `entry_point`, allowing descriptor-only `MODULE.yaml` files (e.g. `vault/`) to coexist without breaking startup.
- **Messaging `ack` path** — Stores the real stream end-byte-offset in cursor so subsequent polls seek past consumed bytes (via new `StorageBackend.get_stream_end_byte_pos`). Replaces the prior `byte_pos=0` that forced full-stream rescans.
- **REPL `/sandbox` and `/strategy`** — Now mutate REPL state and emit `repl.sandbox_changed` / `repl.strategy_changed` audit events instead of printing help.

### Deprecated

### Removed

- **Legacy `modules/pulse/` module** — Functionality migrated to `modules/proactive/`. Per SPEC-017 R-040, no compat shim.
- **Legacy `modules/scheduler/` module** — Same migration path as `pulse`. The `arc agent schedule migrate` CLI command (to be shipped in a follow-up) handles persisted state migration.

### Fixed

- **`ui_reporter/MODULE.yaml`** — Ships with the package; now covered by a regression test.
- **Pre-existing test failures** unrelated to SPEC-017 — `freezegun` test dependency, missing `tomlkit` dep, `threading.Lock` isinstance check broken by Python 3.13, CDP client launch test, stale bio_memory/policy tests needing `session_id`.

### Security

- **OWASP LLM02 / ASI02 / ASI05 / ASI10** — addressed via tool policy pipeline (every tool call audit-logged with agent DID + rule ID), AST validator (9 bypass categories including the CVE-2023-37271 generator-frame bypass and the CVE-2025-68668 ctypes FFI bypass), and deny-by-default egress proxy.
- **NIST 800-53 SI-7(15), CM-5, CM-8** — Federal tier refuses dynamic tool / extension creation at the tool level, BEFORE the loader is consulted. Audit trail captures the denial.
- **Tamper-evident audit trail** — Every policy evaluation, self-modification action, circuit-breaker trip, and completion event emits a structured audit event with agent DID, rule ID, content hash, and timestamps.

## [0.2.0] - 2026-02-21

### Added

- **Biological memory module** — Long-term identity-aware memory system (`bio_memory/`). Tracks agent identity, episodic memory, and working memory across sessions. Includes:
  - `IdentityManager` — Persistent agent identity with traits, preferences, and behavioral patterns.
  - `WorkingMemory` — Session-scoped scratchpad for in-progress reasoning and intermediate state.
  - `Consolidator` — Promotes working memory to long-term episodic storage with relevance scoring.
  - `Retriever` — Context-aware memory retrieval with recency, relevance, and importance weighting.
  - `MODULE.yaml` — Declarative module manifest for Module Bus registration.
- **Shared text sanitizer** — `utils/sanitizer.py` provides `sanitize_text()` with NFKC normalization, zero-width character stripping, and control character removal. Centralizes ASI-06 (Memory & Context Poisoning) defense across all modules.
- **Bio memory CLI commands** — `arc agent bio_memory status|identity|episodes|working` for inspecting biological memory state.
- **Bio memory integration tests** — End-to-end tests for memory lifecycle (write, consolidate, retrieve) and retrieval accuracy.
- **Bio memory unit tests** — Component-level tests for identity manager, working memory, consolidator, retriever, and config.

### Changed

- **Entity extractor** — Refactored `_sanitize_fact_text()` to use shared `sanitize_text()` utility instead of inline implementation. Same defense, less duplication.
- **CLI agent commands** — Registered `bio_memory` as a lazy module group with `status`, `identity`, `episodes`, `working` subcommands.

### Security

- Centralized text sanitization prevents memory poisoning (OWASP ASI-06) with consistent NFKC normalization across entity extraction and biological memory.
- Biological memory validates all writes through the shared sanitizer before storage.

## [0.1.0] - 2026-02-01

### Added

- Initial release with core agent nucleus.
- Ed25519 cryptographic identity with W3C DID format.
- TOML-based configuration with Pydantic validation.
- OpenTelemetry traces, metrics, and structured audit events.
- Token-budgeted context manager with tiered compaction.
- Tool registry with schema validation, policy enforcement, and timeout guards.
- Event-driven module bus for extensibility.
- JSONL session persistence with retention policies.
- Markdown skill discovery and registration.
- Hot-loadable Python extensions.
- Runtime-mutable settings manager.
- Memory module with hybrid search (BM25 + vector), entity extraction, and policy engine.
- Sandboxed filesystem tools (bash, read, write, edit, ls, find, grep).
