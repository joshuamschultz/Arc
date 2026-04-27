# Changelog

All notable changes to ArcAgent (`arc-agent` on PyPI) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-04-26

Major refactor: identity primitives moved to arctrust, dedicated orchestration layer for spawn/sub-runs, four-pillar audit migration to arctrust sinks, and removal of legacy duplicate-named files cluttering the tree.

### Added

- **`arcagent.orchestration` package** — New layer between arcrun (pure loop) and the LLM-facing `delegate` tool. Owns `spawn`, `spawn_many`, `make_spawn_tool`, `RootTokenBudget`, `SpawnResult`, `SpawnSpec`, `TokenUsage`, and `SPAWN_GUIDANCE`. Spawn primitives no longer live in arcrun (`arcrun/builtins/spawn.py` removed). Concern split: arcrun runs one loop, `arcagent.orchestration` spawns sub-loops, `modules/delegate` wraps with policy + identity.
- **Voice / web modules consolidated** — Single `voice_module.py` and `web/url_policy.py` (cleanup of duplicated `*_module 2.py` siblings).
- **Vault audit-gap tests** — `tests/unit/modules/vault/test_resolver_audit_gap.py` and `test_vault_unreachable_audit_event.py` cover the four-pillar audit guarantees.
- **Identity-required tests** — `tests/unit/core/test_identity_required.py` enforces that `ArcAgent.__init__` requires a DID at every tier (ADR-019).
- **Personal-tier global-layer test** — `test_personal_tier_global_layer.py` verifies the policy pipeline still evaluates the global layer at personal tier.
- **Tier metadata test** — `test_tier.py` validates tier-stringency-not-gate semantics (ADR-019).
- **Tool registry DID enforcement test** — `test_tool_registry_did.py` confirms every dispatch carries `caller_did`.
- **UI reporter wiring test** — `test_ui_reporter_wiring.py` regression-tests the dashboard event hook.
- **Voice all-tiers audit test** — `test_voice_audit_all_tiers.py` verifies voice module audits at personal/enterprise/federal.
- **Web deny-by-default test** — `test_web_deny_by_default.py` confirms web module fails closed without explicit allowlist.

### Changed

- **Identity primitives moved to arctrust** — `core/identity.py` removed; `AgentIdentity`, `ChildIdentity`, `derive_child_identity`, `generate_did`, `parse_did`, `validate_did` now live in `arctrust.identity`. arcagent imports from arctrust. Eliminates the latent circular dependency documented in SPEC-018 §HIGH-1.
- **Trust store moved to arctrust** — `core/trust_store.py` and `utils/trust_store.py` removed; `load_operator_pubkey`, `load_issuer_pubkey`, `TrustStoreError`, `invalidate_cache` now in `arctrust.trust_store`.
- **Audit emission migrated to arctrust** — All security-relevant audit events now route through `arctrust.audit.emit(AuditEvent, sink)`. `JsonlSink` for compliance, `SignedChainSink` for tamper-evident chain, `arcui.bridge.UIBridgeSink` for live observability. Single emission point, sinks fan out per ADR-019.
- **Tool policy pipeline migrated to arctrust** — `core/tool_policy.py` shrunk from 614 LOC to a thin shim around `arctrust.policy.PolicyPipeline`. `Decision`, `PolicyLayer`, `ToolCall`, `PolicyContext`, `TierConfig`, `build_pipeline` all sourced from arctrust.
- **`ArcAgent.__init__` requires DID** — Identity is now mandatory at every tier, not just federal. Implements ADR-019 four-pillar universality.
- **`ToolRegistry` carries `caller_did`** — Every dispatch records the calling DID for the policy pipeline and audit trail.
- **Module-bus / extension API hardening** — Tighter typing across `module_bus.py`, `extensions.py`, `skill_registry.py`, `tool_registry.py`.
- **Browser, delegate, scheduler, planning, vault, voice, web modules** — Cleanup pass; legacy duplicate-named files removed; tighter audit emission paths.
- **README rewritten** — 385-line marketing prose replaced with focused layer-position + public-surface reference (under 100 lines).

### Removed

- **`core/identity.py`** — Migrated to arctrust. Re-export shim removed; callers must import from `arctrust`.
- **`core/trust_store.py`, `utils/trust_store.py`** — Migrated to arctrust.
- **Duplicate `* 2.py`, `* 2.yaml` files** — Cleanup of accidentally-checked-in macOS Finder duplicates across `delegate/`, `memory_acl/`, `user_profile/`, `voice/`, `web/`, `skill_improver/nudge/`, `tool_policy_layers 2.py`, `browser/`. No functional change.
- **`docs/voice-air-gap-setup 2.md`** — Stray duplicate doc.

### Security

- **ADR-019 Four Pillars Universal** — Identity, Sign, Authorize, Audit now enforced at every tier. Personal/enterprise/federal differ only in stringency (FIPS crypto, signed allowlists, layer count) — never in whether the pillar applies.
- **Audit single-point-of-emission** — All security events flow through `arctrust.audit.emit`; no module emits directly. Removes risk of schema drift across callers.

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
