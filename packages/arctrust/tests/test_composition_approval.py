"""SPEC-035 sub-scope B — LIVE forbidden-composition gate + operator approval.

The GlobalLayer now enforces ``forbidden_compositions`` against the union of a
call's ``capability_tags`` and the session's accumulated legs. A forbidden
composition denies unless the call carries a valid, one-shot, operator-signed
``ApprovalGrant`` (ASI09 — the agent can never self-approve).
"""

from __future__ import annotations

from arctrust.identity import AgentIdentity
from arctrust.policy import (
    ApprovalGrant,
    GlobalLayer,
    PolicyContext,
    PolicyPipeline,
    ToolCall,
    build_pipeline,
    sign_approval,
    sign_call,
    verify_approval,
    verify_call,
)


def _call(
    *,
    agent_did: str,
    tool_name: str = "read_file",
    arguments: dict[str, object] | None = None,
    capability_tags: frozenset[str] = frozenset(),
    approval: ApprovalGrant | None = None,
) -> ToolCall:
    return ToolCall(
        tool_name=tool_name,
        arguments=arguments or {"path": "/tmp/x"},
        agent_did=agent_did,
        session_id="sess-001",
        classification="UNCLASSIFIED",
        capability_tags=capability_tags,
        approval=approval,
    )


def _ctx(session_capabilities: frozenset[str] | None = None) -> PolicyContext:
    return PolicyContext(
        tier="personal",
        policy_version="1.0",
        bundle_age_seconds=0.0,
        session_capabilities=session_capabilities,
    )


# ---------------------------------------------------------------------------
# New optional fields — defaults + backward-compatible signing
# ---------------------------------------------------------------------------


def test_tool_call_new_fields_default_empty() -> None:
    call = ToolCall(
        tool_name="t",
        arguments={},
        agent_did="did:arc:test:exec/aabbccdd",
        session_id="s",
        classification="UNCLASSIFIED",
    )
    assert call.capability_tags == frozenset()
    assert call.approval is None


def test_policy_context_session_capabilities_default_none() -> None:
    ctx = PolicyContext(tier="personal", policy_version="1.0", bundle_age_seconds=0.0)
    assert ctx.session_capabilities is None


def test_capability_tags_construct() -> None:
    call = _call(
        agent_did="did:arc:test:exec/aabbccdd", capability_tags=frozenset({"private_data"})
    )
    assert call.capability_tags == frozenset({"private_data"})


def test_signing_bytes_excludes_new_fields() -> None:
    """capability_tags/approval are attestation metadata, NOT signed content."""
    agent = AgentIdentity.generate(org="test", agent_type="exec")
    base = _call(agent_did=agent.did)
    tagged = _call(
        agent_did=agent.did, capability_tags=frozenset({"private_data", "external_comms"})
    )
    assert base.signing_bytes() == tagged.signing_bytes()


def test_signed_call_with_capability_tags_still_verifies() -> None:
    """Signing a call carrying capability_tags leaves verify_call True — proving
    the new field is excluded from signing_bytes()."""
    agent = AgentIdentity.generate(org="test", agent_type="exec")
    call = make_signed_with_tags(agent)
    assert verify_call(call) is True
    assert call.capability_tags == frozenset({"private_data"})


def make_signed_with_tags(agent: AgentIdentity) -> ToolCall:
    call = _call(agent_did=agent.did, capability_tags=frozenset({"private_data"}))
    return sign_call(call, agent)


# ---------------------------------------------------------------------------
# LIVE forbidden-composition gate
# ---------------------------------------------------------------------------


def _global() -> GlobalLayer:
    return GlobalLayer(
        deny_rules={},
        forbidden_compositions=[frozenset({"a", "b", "c"})],
    )


async def test_partial_union_allows() -> None:
    layer = _global()
    call = _call(agent_did="did:arc:test:exec/aabbccdd", capability_tags=frozenset({"a"}))
    decision = await layer.evaluate(call, _ctx(session_capabilities=frozenset({"b"})))
    assert decision.outcome == "allow"


async def test_complete_union_denies() -> None:
    layer = _global()
    call = _call(agent_did="did:arc:test:exec/aabbccdd", capability_tags=frozenset({"c"}))
    decision = await layer.evaluate(call, _ctx(session_capabilities=frozenset({"a", "b"})))
    assert decision.outcome == "deny"
    assert decision.rule_id == "global.forbidden_composition"
    assert decision.layer == "global"


async def test_deny_rules_take_precedence_over_composition() -> None:
    layer = GlobalLayer(
        deny_rules={"read_file": "file access denied"},
        forbidden_compositions=[frozenset({"a", "b", "c"})],
    )
    # Union does NOT complete the forbidden set, but the tool is denylisted.
    call = _call(agent_did="did:arc:test:exec/aabbccdd", capability_tags=frozenset({"a"}))
    decision = await layer.evaluate(call, _ctx(session_capabilities=frozenset({"b"})))
    assert decision.outcome == "deny"
    assert decision.rule_id == "global.denylist"


# ---------------------------------------------------------------------------
# Operator approval — one-shot, operator-signed, agent-cannot-self-approve
# ---------------------------------------------------------------------------


async def test_valid_operator_approval_allows_forbidden_composition() -> None:
    agent = AgentIdentity.generate(org="test", agent_type="exec")
    operator = AgentIdentity.generate(org="test", agent_type="operator")
    layer = _global()
    unapproved = _call(agent_did=agent.did, capability_tags=frozenset({"c"}))
    grant = sign_approval(unapproved, operator)
    approved = _call(agent_did=agent.did, capability_tags=frozenset({"c"}), approval=grant)
    decision = await layer.evaluate(approved, _ctx(session_capabilities=frozenset({"a", "b"})))
    assert decision.outcome == "allow"


def test_verify_approval_true_for_operator_signed() -> None:
    agent = AgentIdentity.generate(org="test", agent_type="exec")
    operator = AgentIdentity.generate(org="test", agent_type="operator")
    call = _call(agent_did=agent.did, capability_tags=frozenset({"c"}))
    grant = sign_approval(call, operator)
    approved = call.model_copy(update={"approval": grant})
    assert verify_approval(approved, grant) is True


async def test_self_approval_rejected() -> None:
    """Approval signed by the AGENT's own identity is rejected (ASI09)."""
    agent = AgentIdentity.generate(org="test", agent_type="exec")
    layer = _global()
    call = _call(agent_did=agent.did, capability_tags=frozenset({"c"}))
    self_grant = sign_approval(call, agent)  # approver_did == agent_did
    assert verify_approval(call, self_grant) is False
    approved = _call(agent_did=agent.did, capability_tags=frozenset({"c"}), approval=self_grant)
    decision = await layer.evaluate(approved, _ctx(session_capabilities=frozenset({"a", "b"})))
    assert decision.outcome == "deny"
    assert decision.rule_id == "global.forbidden_composition"


async def test_approval_bound_to_different_call_rejected() -> None:
    """An approval minted for a DIFFERENT call (different arguments) does not
    transfer — one-shot binding fails, a second distinct call re-denies."""
    agent = AgentIdentity.generate(org="test", agent_type="exec")
    operator = AgentIdentity.generate(org="test", agent_type="operator")
    layer = _global()
    first = _call(agent_did=agent.did, arguments={"path": "/a"}, capability_tags=frozenset({"c"}))
    grant = sign_approval(first, operator)
    # Same tags, DIFFERENT arguments → different call_hash → grant does not bind.
    second = _call(
        agent_did=agent.did,
        arguments={"path": "/b"},
        capability_tags=frozenset({"c"}),
        approval=grant,
    )
    assert verify_approval(second, grant) is False
    decision = await layer.evaluate(second, _ctx(session_capabilities=frozenset({"a", "b"})))
    assert decision.outcome == "deny"
    assert decision.rule_id == "global.forbidden_composition"


# ---------------------------------------------------------------------------
# Cache must never defeat the one-shot approval flow (SPEC-035)
# ---------------------------------------------------------------------------


def _forbidden_pipeline() -> PolicyPipeline:
    # personal tier → identity + global only. cache_ttl>0 so decisions cache.
    return build_pipeline(
        tier="personal",
        forbidden_compositions=[frozenset({"a", "b"})],
        cache_ttl_seconds=300.0,
    )


async def test_approval_bearing_call_bypasses_cached_deny() -> None:
    """A forbidden-composition DENY is cached, but re-dispatching the SAME call
    now carrying a valid operator approval must NOT be served the cached DENY —
    the approval is excluded from the cache key, so the pipeline must skip cache
    for approval-bearing calls and re-evaluate through GlobalLayer to ALLOW."""
    agent = AgentIdentity.generate(org="test", agent_type="exec")
    operator = AgentIdentity.generate(org="test", agent_type="operator")
    pipeline = _forbidden_pipeline()
    ctx = _ctx(session_capabilities=frozenset({"a", "b"}))

    # capability_tags complete the forbidden set → DENY, and this caches it.
    denied = sign_call(_call(agent_did=agent.did, capability_tags=frozenset({"a", "b"})), agent)
    first = await pipeline.evaluate(denied, ctx)
    assert first.outcome == "deny"
    assert first.rule_id == "global.forbidden_composition"

    # Same signed call, now carrying a valid operator approval.
    grant = sign_approval(denied, operator)
    approved = denied.model_copy(update={"approval": grant})
    second = await pipeline.evaluate(approved, ctx)
    assert second.outcome == "allow", "cached DENY must not defeat one-shot approval"


async def test_partial_then_completed_ledger_does_not_stale_cache_hit() -> None:
    """A fixed-argument call ALLOWed under a PARTIAL ledger must not be served
    that cached ALLOW once the ledger completes the forbidden set.

    ``session_capabilities`` is a security-relevant input to the GlobalLayer
    composition check; omitting it from the cache key lets an egress tool with
    constant arguments replay a partial-ledger ALLOW and skip the completed-set
    DENY. Folding the accumulated legs into the key forces re-evaluation."""
    agent = AgentIdentity.generate(org="test", agent_type="exec")
    pipeline = _forbidden_pipeline()  # forbidden {"a", "b"}
    # Same signed call each time (constant tool + arguments → constant call hash).
    call = sign_call(
        _call(agent_did=agent.did, tool_name="egress", capability_tags=frozenset({"b"})),
        agent,
    )

    # Partial ledger holds only {} → union {"b"} does not complete {"a","b"} → ALLOW.
    partial = await pipeline.evaluate(call, _ctx(session_capabilities=frozenset()))
    assert partial.outcome == "allow"

    # Ledger now carries {"a"} → union {"a","b"} completes the forbidden set → DENY.
    completed = await pipeline.evaluate(call, _ctx(session_capabilities=frozenset({"a"})))
    assert completed.outcome == "deny", "stale cache must not hide the completed-set DENY"
    assert completed.rule_id == "global.forbidden_composition"


async def test_normal_repeat_still_cache_hits() -> None:
    """Approval-less calls keep the existing cache behavior: a second identical
    evaluation is a cache hit (proves the fix only bypasses cache for approvals)."""
    agent = AgentIdentity.generate(org="test", agent_type="exec")
    pipeline = build_pipeline(tier="personal", cache_ttl_seconds=300.0)
    ctx = _ctx()

    events: list[bool] = []
    pipeline._audit_sink = lambda _e, payload: events.append(bool(payload["cache_hit"]))  # type: ignore[attr-defined]

    call = sign_call(_call(agent_did=agent.did), agent)
    first = await pipeline.evaluate(call, ctx)
    second = await pipeline.evaluate(call, ctx)
    assert first.outcome == "allow"
    assert second.outcome == "allow"
    assert events == [False, True], "second identical approval-less call must cache-hit"
