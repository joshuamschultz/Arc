---
id: ADR-017A
title: Opt-in Policy Pipeline at Registry Construction
status: accepted
date: 2026-04-18
spec: SPEC-017
tags:
  - policy
  - tool-registry
  - backward-compatibility
  - security
---

# ADR-017A: Opt-in Policy Pipeline at Registry Construction

## Status

Accepted (2026-04-18). Implemented in `arcagent/core/tool_registry.py`.

## Context

SPEC-017 specifies the 5-layer tool policy pipeline (Global → Provider → Agent → Team → Sandbox) as the authoritative deny path — every tool call MUST flow through it (R-010, R-011).

The existing `ToolRegistry` has >500 dependent unit and integration tests. Many of them construct a registry with no policy context whatsoever (fixtures for isolated tool tests). A full cut-over to mandatory pipeline would break ~100 tests and force every caller to construct a full pipeline just to register a tool — a significant regression in developer ergonomics for test fixtures.

## Decision

`ToolRegistry(policy_pipeline=None)` is an *optional* constructor argument.

- When `policy_pipeline` is provided, every dispatch goes through it first. `PolicyDenied` is raised on deny; the tool's `execute()` is never reached. Fail-closed on layer exceptions.
- When `policy_pipeline` is absent, the registry runs permissively (only the existing registration-time `_check_policy` denylist applies).
- Call sites that require enforcement (production agent startup, `ArcAgent._ensure_model` path) pass one explicitly.
- Call sites that don't care (isolated unit tests, tool-scaffolding scripts) omit it.

Tier + policy version propagate through the same construction path — `tier` and `policy_version` are `__init__` args that become fields of every `PolicyContext` the registry produces.

## Consequences

### Positive

- **Backward compatibility preserved** — existing tests and call sites continue to work without modification.
- **Enforcement is wire-time config, not code-time assumption** — which tier applies, which rules are active, is entirely a property of how `ArcAgent` wires the registry.
- **Test surface unchanged** — fixtures that construct a registry for tool-level testing don't need to mock the pipeline.

### Negative

- Enforcement correctness depends on the wiring layer passing a pipeline. A misconfigured `ArcAgent` could instantiate a registry without one and silently run permissively.
- Two code paths (registration-time `_check_policy` + dispatch-time pipeline) coexist until a future cleanup.

### Mitigations

- `ArcAgent._ensure_model` (the ONLY production path) always constructs with a pipeline. Regression tests guard this.
- Integration test `test_registry_propagates_tier_to_policy_context` ensures the tier flow works end-to-end.

## Alternatives considered

- **Mandatory pipeline argument** — rejected. 100+ test updates for no security gain (tests already run without network/tool side effects).
- **Two separate classes (`ToolRegistry` + `EnforcingToolRegistry`)** — rejected. Duplicates the dispatch machinery; harder to keep in sync.
- **Pipeline as a module-level singleton** — rejected. Violates modularity; hidden global state.

## References

- SPEC-017 §R-010, R-011
- SDD §3.6 (registry + policy integration)
- Review report §Pillar 3 (tier regression finding)
