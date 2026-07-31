---
id: ADR-017D
title: Tier Flows Through Registry Construction, Not Per-Call
status: accepted
date: 2026-04-18
spec: SPEC-017
tags:
  - tier
  - tool-registry
  - policy
  - audit-trail
---

# ADR-017D: Tier Flows Through Registry Construction, Not Per-Call

## Status

Accepted (2026-04-18). Implemented after `/review` caught the tier-hardcoded-to-personal bug.

## Context

SPEC-017 distinguishes three operational tiers:

- **Federal** — 5 policy layers, dynamic code DENIED, hard turn/cost caps
- **Enterprise** — 4 policy layers (no Team), approval gates
- **Personal** — 1 policy layer (Global only), everything allowed

Tier affects **two** places in the dispatch path:

1. **Pipeline composition** — which `PolicyLayer` instances are active (factory-time decision, `build_pipeline(tier=...)`)
2. **PolicyContext content** — `ctx.tier` is recorded in audit events + read by layers during evaluation

Initial implementation set tier at (1) via the factory but HARDCODED `"personal"` at (2) inside `ToolRegistry._create_wrapped_execute`. Result:

- Enforcement correctness was fine (layers evaluate correctly because they are tier-selected)
- But **audit trails were wrong** — federal deployments recorded `tier="personal"` in every `policy.evaluate` event

This is a subtle bug: it doesn't break enforcement, but it invalidates the audit trail's claim that "every event is truthful". Per CLAUDE.md: "The audit trail must be sufficient to rebuild state."

## Decision

Tier is set **once** at `ToolRegistry` construction:

```python
ToolRegistry(
    config=...,
    bus=...,
    telemetry=...,
    policy_pipeline=pipeline,
    tier="federal",                 # ← construction-time
    policy_version="v2.7.1",        # ← construction-time
)
```

Every `PolicyContext` the registry builds uses the registry's tier. The wiring layer (`ArcAgent._ensure_model` or equivalent) passes the SAME tier to both `build_pipeline(tier=...)` and `ToolRegistry(tier=...)`.

A regression test asserts this propagates:

```python
def test_registry_propagates_tier_to_policy_context():
    reg = ToolRegistry(..., tier="federal", policy_version="v2.7.1")
    # Recording layer captures every ctx it sees
    await wrapped_tool_call()
    assert contexts[0].tier == "federal"
    assert contexts[0].policy_version == "v2.7.1"
```

## Consequences

### Positive

- **Audit trail is truthful** — every evaluation records the deployment's actual tier.
- **One source of truth** — tier flows from config → factory + registry → every downstream record.
- **Cannot drift at runtime** — tier is read once, stored, never mutated.

### Negative

- **Tier changes require agent restart.** Not a negative in practice — tier is a deployment-level decision, not a runtime toggle. Changing tiers mid-operation is an operational footgun anyway.

### Mitigations

- Runbook documents the restart requirement.
- `arc agent policy layers --tier=...` CLI (Phase 8) lets operators verify the intended tier without restarting.

## Design principle captured

**Tier-like settings (deployment context, static across a process lifetime) must live in the construction signature, not be supplied per-call.**

Per-call supply invites:
- Hardcoded defaults that lie
- Bugs where one code path supplies the real value and another supplies a fallback
- Audit trails that depend on correct threading through every caller

Construction-time supply gives the compiler a single place to check + a single place for regression tests to guard.

## Alternatives considered

- **Read tier from a global** — rejected. Hidden state; hard to test.
- **Read tier from env var** — rejected. Doesn't match the config-driven philosophy (CLAUDE.md).
- **Require tier per tool call** — rejected. Every caller would need tier awareness; most don't.

## References

- SPEC-017 R-010 (tier-aware layer composition)
- SPEC-017 R-060 (audit events must include tier)
- Review report §Pillar 3 (original finding)
- `test_registry_propagates_tier_to_policy_context` in `tests/unit/core/test_tool_registry.py`
