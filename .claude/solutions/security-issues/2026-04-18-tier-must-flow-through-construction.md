---
title: "Tier / Deployment-Context Values Must Flow Through Construction, Not Per-Call"
category: security-issues
date: 2026-04-18
tags:
  - audit-trail
  - tier
  - configuration
  - tool-registry
  - policy
module: arcagent.core.tool_registry
symptom: "Audit trails recorded ``tier=personal`` in federal deployments. Enforcement was correct; audit events lied."
root_cause: "The per-call PolicyContext was built with a hardcoded default tier. The factory that chose which policy layers to use saw the real tier; the context that annotated every audit event saw the fallback."
severity: medium
spec_id: SPEC-017
resolution_verified: true
tests_passing: 95
---

# Tier / Deployment-Context Values Must Flow Through Construction, Not Per-Call

## When this pattern applies

You have a config value that is **constant over a process lifetime**
but **consumed by many call sites**. Examples: deployment tier,
FedRAMP boundary, compliance mode, feature flags that ship with the
binary, billing plan.

You are building a central component (registry, router, dispatcher)
that takes the value at instantiation and should pass it to every
downstream record.

## The mistake

```python
# BEFORE — tier hardcoded in the per-call path
class ToolRegistry:
    def __init__(self, pipeline, *, agent_did: str = "unknown"):
        self._pipeline = pipeline
        self._agent_did = agent_did

    def _create_wrapped_execute(self, tool):
        async def wrapped_execute(args, **kw):
            call = ToolCall(...)
            ctx = PolicyContext(
                tier="personal",      # ← hardcoded default
                policy_version="v0",  # ← hardcoded default
                bundle_age_seconds=0.0,
            )
            decision = await self._pipeline.evaluate(call, ctx)
            ...
```

Reasoning that looks fine at review time:

- "Tier is already selected at pipeline construction (`build_pipeline(tier=...)`) — the correct layers are active. The context tier is just for audit."
- "Federal deployments wire the pipeline with all 5 layers; personal deployments wire with 1. Enforcement can't be wrong."
- "If someone needs a different tier in the audit, they can pass it through as a per-call arg later."

## Why this fails

**Enforcement and audit fall out of sync.** In production:

- The `GlobalLayer` + `ProviderLayer` + `AgentLayer` + `TeamLayer` + `SandboxLayer` correctly reject privileged calls (because they were wired for federal at factory time)
- But every `policy.evaluate` audit event records `tier: "personal"` because the context said so
- When an auditor reconstructs the deployment's behavior from the log alone, they see a **personal-tier audit trail for a federal-tier deployment**

This breaks CLAUDE.md's invariant: "The audit trail must be sufficient
to rebuild state. Treat the log as an event stream: given the audit
log and a clean install, you can reconstruct the system's state at
any prior point in time."

It's also a **compliance failure.** NIST 800-53 AU-2 requires that
audit records reflect the actual security posture of the system at
the time of the event. An audit stream that systematically lies about
the tier is not compliant — even if the underlying enforcement is.

## The solution — construction-time context

```python
# AFTER — tier flows through construction
class ToolRegistry:
    def __init__(
        self,
        pipeline,
        *,
        agent_did: str = "unknown",
        tier: Literal["federal", "enterprise", "personal"] = "personal",
        policy_version: str = "v0",
    ):
        self._pipeline = pipeline
        self._agent_did = agent_did
        self._tier = tier
        self._policy_version = policy_version

    def _create_wrapped_execute(self, tool):
        tier = self._tier  # captured in closure
        policy_version = self._policy_version

        async def wrapped_execute(args, **kw):
            call = ToolCall(...)
            ctx = PolicyContext(
                tier=tier,
                policy_version=policy_version,
                bundle_age_seconds=0.0,
            )
            ...
```

And the wiring layer passes the SAME tier to BOTH the factory and the
registry:

```python
# ArcAgent.startup():
pipeline = build_pipeline(tier=config.tier)  # layers
registry = ToolRegistry(
    pipeline=pipeline,
    tier=config.tier,                        # same tier — audit truth
    policy_version=config.policy_version,
)
```

## Regression test

```python
async def test_registry_propagates_tier_to_policy_context():
    contexts = []

    class _CtxRecorder:
        name = "recorder"
        async def evaluate(self, call, ctx):
            contexts.append(ctx)
            return Decision.allow(...)

    pipeline = ToolPolicyPipeline(layers=[_CtxRecorder()])
    reg = ToolRegistry(
        policy_pipeline=pipeline,
        tier="federal",
        policy_version="v2.7.1",
    )
    reg.register(tool)
    await reg._create_wrapped_execute(tool)()

    assert contexts[0].tier == "federal"
    assert contexts[0].policy_version == "v2.7.1"
```

Without this test, the defaults (`"personal"`, `"v0"`) would silently
reappear on any refactor.

## Design principle

**Tier-like settings must live in the construction signature, not be
supplied per-call.** Deployment context is stable for the process
lifetime; making it a parameter invites:

- Hardcoded defaults that silently lie
- Bugs where one code path threads the real value and another threads
  the fallback
- Audit trails that depend on correct threading through every caller

Construction-time supply gives the compiler a single place to check +
a single place for regression tests to guard.

## Cost

- Tier changes require an agent restart.

## Tradeoff

Small operational cost (restart on tier change) in exchange for an
audit trail that is truthful by construction. The restart itself
matches operational reality — changing a deployment's tier mid-flight
is already a footgun (what happens to in-flight sessions under a
stricter tier?).

## Applicability beyond SPEC-017

This pattern applies whenever you have a value with these properties:

1. **Set once at startup** — comes from config, env, build arg
2. **Read many times** — per-call recording, per-request evaluation
3. **Audit-critical** — someone trusts records to reflect reality

Tier is one example. Others: FedRAMP boundary, data-classification
context, tenant ID in multi-tenant systems, feature-flag snapshot.

## Verification

- `test_registry_propagates_tier_to_policy_context` in `tests/unit/core/test_tool_registry.py`
- ADR-017D formalizes the decision
- All 95 tool_registry + tool_policy tests pass
