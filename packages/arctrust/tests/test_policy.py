"""Tests for arctrust.policy — pipeline, decision, layer protocol, tier config."""

from __future__ import annotations

import time
from typing import Any

import pytest

from arctrust.identity import AgentIdentity
from arctrust.policy import (
    Decision,
    PolicyContext,
    PolicyLayer,
    PolicyPipeline,
    ProviderUsage,
    TierConfig,
    ToolCall,
    ToolRuntimeStatus,
    build_pipeline,
    sign_call,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_call(
    tool_name: str = "read_file",
    agent_did: str = "did:arc:test:exec/aabbccdd",
    classification: str = "UNCLASSIFIED",
    session_id: str = "sess-001",
) -> ToolCall:
    return ToolCall(
        tool_name=tool_name,
        arguments={"path": "/tmp/test"},
        agent_did=agent_did,
        session_id=session_id,
        classification=classification,
    )


def make_ctx(
    tier: str = "personal",
    bundle_age: float = 0.0,
) -> PolicyContext:
    # Clean provider/runtime state so the now-real Provider/Sandbox layers pass
    # through at enterprise/federal (they fail closed on missing state); tests
    # here exercise identity/global/agent behavior, not budget/isolation.
    return PolicyContext(
        tier=tier,  # type: ignore[arg-type]
        policy_version="1.0",
        bundle_age_seconds=bundle_age,
        provider_usage=ProviderUsage(
            provider="anthropic", tokens_used=0, cost_used=0.0, requests_in_window=0
        ),
        tool_runtime=ToolRuntimeStatus(
            verified=True, required_isolation="host", available_isolation="host"
        ),
    )


class AllowLayer:
    name = "allow_all"

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        return Decision.allow(input_hash="abc", evaluated_at_us=int(time.monotonic() * 1_000_000))


class DenyLayer:
    name = "deny_all"

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        return Decision.deny(
            layer=self.name,
            rule_id="deny_all.rule",
            reason="always deny",
            input_hash="abc",
            evaluated_at_us=int(time.monotonic() * 1_000_000),
        )


class ExplodingLayer:
    name = "exploder"

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        raise RuntimeError("layer error")


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


class TestDecision:
    def test_allow_decision(self) -> None:
        d = Decision.allow(input_hash="hash", evaluated_at_us=100)
        assert d.outcome == "allow"
        assert not d.is_deny()

    def test_deny_decision(self) -> None:
        d = Decision.deny(
            layer="global",
            rule_id="rule1",
            reason="blocked",
            input_hash="hash",
            evaluated_at_us=100,
        )
        assert d.outcome == "deny"
        assert d.is_deny()
        assert d.layer == "global"
        assert d.rule_id == "rule1"
        assert d.reason == "blocked"

    def test_frozen(self) -> None:
        d = Decision.allow(input_hash="h", evaluated_at_us=0)
        with pytest.raises(Exception):  # noqa: B017 — testing that any exception fires on frozen-model mutation
            d.outcome = "deny"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PolicyPipeline — core behavior
# ---------------------------------------------------------------------------


class TestPipelineFirstDenyWins:
    async def test_all_allow_returns_allow(self) -> None:
        pipeline = PolicyPipeline(layers=[AllowLayer(), AllowLayer()])
        result = await pipeline.evaluate(make_call(), make_ctx())
        assert result.outcome == "allow"

    async def test_single_deny_returns_deny(self) -> None:
        pipeline = PolicyPipeline(layers=[DenyLayer()])
        result = await pipeline.evaluate(make_call(), make_ctx())
        assert result.outcome == "deny"

    async def test_deny_layer_first_short_circuits(self) -> None:
        """First DENY wins — subsequent layers are not evaluated."""
        call_order: list[str] = []

        class TrackingAllow:
            name = "tracking_allow"

            async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
                call_order.append(self.name)
                return Decision.allow(
                    input_hash="h",
                    evaluated_at_us=int(time.monotonic() * 1_000_000),
                )

        class TrackingDeny:
            name = "tracking_deny"

            async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
                call_order.append(self.name)
                return Decision.deny(
                    layer=self.name,
                    rule_id="r",
                    reason="stop",
                    input_hash="h",
                    evaluated_at_us=int(time.monotonic() * 1_000_000),
                )

        layer_after = TrackingAllow()
        layer_after.name = "after_deny"

        pipeline = PolicyPipeline(layers=[TrackingAllow(), TrackingDeny(), layer_after])
        result = await pipeline.evaluate(make_call(), make_ctx())
        assert result.outcome == "deny"
        assert "after_deny" not in call_order

    async def test_no_layers_allows(self) -> None:
        """Pipeline with no layers should allow (vacuously true)."""
        pipeline = PolicyPipeline(layers=[])
        result = await pipeline.evaluate(make_call(), make_ctx())
        assert result.outcome == "allow"


class TestPipelineFailClosed:
    async def test_layer_exception_becomes_deny(self) -> None:
        """Any exception in a layer is treated as DENY — fail-closed."""
        pipeline = PolicyPipeline(layers=[ExplodingLayer()])
        result = await pipeline.evaluate(make_call(), make_ctx())
        assert result.outcome == "deny"
        assert result.rule_id == "layer_error"

    async def test_exception_layer_before_allow_still_denies(self) -> None:
        pipeline = PolicyPipeline(layers=[ExplodingLayer(), AllowLayer()])
        result = await pipeline.evaluate(make_call(), make_ctx())
        assert result.outcome == "deny"

    async def test_allow_then_exception_denies(self) -> None:
        pipeline = PolicyPipeline(layers=[AllowLayer(), ExplodingLayer()])
        result = await pipeline.evaluate(make_call(), make_ctx())
        assert result.outcome == "deny"


class TestPipelineShadowMode:
    async def test_shadow_forces_allow_on_deny(self) -> None:
        """In shadow mode, decisions are evaluated but always return allow."""
        pipeline = PolicyPipeline(layers=[DenyLayer()], shadow=True)
        result = await pipeline.evaluate(make_call(), make_ctx())
        assert result.outcome == "allow"

    async def test_shadow_allow_stays_allow(self) -> None:
        pipeline = PolicyPipeline(layers=[AllowLayer()], shadow=True)
        result = await pipeline.evaluate(make_call(), make_ctx())
        assert result.outcome == "allow"


class TestPipelineRestrictedMode:
    async def test_stale_bundle_denies_non_safe_tool(self) -> None:
        pipeline = PolicyPipeline(
            layers=[AllowLayer()],
            max_bundle_age_seconds=60.0,
            safe_set={"safe_tool"},
        )
        ctx = make_ctx(bundle_age=120.0)  # older than max
        call = make_call(tool_name="dangerous_tool")
        result = await pipeline.evaluate(call, ctx)
        assert result.outcome == "deny"
        assert result.rule_id == "restricted_mode"

    async def test_stale_bundle_allows_safe_tool(self) -> None:
        pipeline = PolicyPipeline(
            layers=[AllowLayer()],
            max_bundle_age_seconds=60.0,
            safe_set={"safe_tool"},
        )
        ctx = make_ctx(bundle_age=120.0)
        call = make_call(tool_name="safe_tool")
        result = await pipeline.evaluate(call, ctx)
        assert result.outcome == "allow"

    async def test_fresh_bundle_bypasses_restricted(self) -> None:
        pipeline = PolicyPipeline(
            layers=[AllowLayer()],
            max_bundle_age_seconds=60.0,
            safe_set={"safe_tool"},
        )
        ctx = make_ctx(bundle_age=30.0)  # fresh
        call = make_call(tool_name="any_tool")
        result = await pipeline.evaluate(call, ctx)
        assert result.outcome == "allow"


class TestPipelineCache:
    async def test_cache_returns_same_decision(self) -> None:
        call_count = 0

        class CountingAllow:
            name = "counting"

            async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
                nonlocal call_count
                call_count += 1
                return Decision.allow(
                    input_hash="h",
                    evaluated_at_us=int(time.monotonic() * 1_000_000),
                )

        pipeline = PolicyPipeline(layers=[CountingAllow()], cache_ttl_seconds=30.0)
        call = make_call()
        ctx = make_ctx()
        await pipeline.evaluate(call, ctx)
        await pipeline.evaluate(call, ctx)
        # Second evaluation hits cache — layer called only once
        assert call_count == 1

    async def test_different_tool_bypasses_cache(self) -> None:
        call_count = 0

        class CountingAllow:
            name = "counting"

            async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
                nonlocal call_count
                call_count += 1
                return Decision.allow(
                    input_hash="h",
                    evaluated_at_us=int(time.monotonic() * 1_000_000),
                )

        pipeline = PolicyPipeline(layers=[CountingAllow()], cache_ttl_seconds=30.0)
        ctx = make_ctx()
        await pipeline.evaluate(make_call(tool_name="tool_a"), ctx)
        await pipeline.evaluate(make_call(tool_name="tool_b"), ctx)
        assert call_count == 2


class TestPipelineAuditSink:
    async def test_audit_sink_called_on_evaluate(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        def sink(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        pipeline = PolicyPipeline(layers=[AllowLayer()], audit_sink=sink)
        await pipeline.evaluate(make_call(), make_ctx())
        assert len(events) == 1
        assert events[0][0] == "policy.evaluate"
        assert events[0][1]["decision"] == "allow"

    async def test_audit_sink_exception_does_not_break_evaluation(self) -> None:
        def bad_sink(event_type: str, payload: dict[str, Any]) -> None:
            raise RuntimeError("sink exploded")

        pipeline = PolicyPipeline(layers=[AllowLayer()], audit_sink=bad_sink)
        # Must not raise
        result = await pipeline.evaluate(make_call(), make_ctx())
        assert result.outcome == "allow"


# ---------------------------------------------------------------------------
# PolicyLayer Protocol
# ---------------------------------------------------------------------------


class TestPolicyLayerProtocol:
    def test_allow_layer_satisfies_protocol(self) -> None:
        layer = AllowLayer()
        assert isinstance(layer, PolicyLayer)

    def test_deny_layer_satisfies_protocol(self) -> None:
        layer = DenyLayer()
        assert isinstance(layer, PolicyLayer)


# ---------------------------------------------------------------------------
# TierConfig
# ---------------------------------------------------------------------------


class TestTierConfig:
    def test_personal_tier_config(self) -> None:
        tc = TierConfig.for_tier("personal")
        assert tc.tier == "personal"
        assert tc.max_parallel_tools >= 1

    def test_enterprise_tier_config(self) -> None:
        tc = TierConfig.for_tier("enterprise")
        assert tc.tier == "enterprise"

    def test_federal_tier_config(self) -> None:
        tc = TierConfig.for_tier("federal")
        assert tc.tier == "federal"
        # Federal caps parallel HTTPS tools at 4 per SPEC-017 R-025
        assert tc.max_parallel_tools <= 4

    def test_invalid_tier_raises(self) -> None:
        with pytest.raises(ValueError):
            TierConfig.for_tier("unknown")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# build_pipeline factory
# ---------------------------------------------------------------------------


class TestBuildPipeline:
    """Pipelines now prepend a fail-closed IdentityLayer at every tier, so
    behavioral tests sign their calls and (for enterprise/federal) register the
    signer in ``agent_registry`` — otherwise the call is denied before reaching
    the layer under test. That admission gate is itself covered in
    test_identity_layer.py."""

    @staticmethod
    def _signed(ident: AgentIdentity, tool_name: str = "read_file") -> ToolCall:
        return sign_call(make_call(tool_name=tool_name, agent_did=ident.did), ident)

    async def test_personal_pipeline_has_identity_then_global(self) -> None:
        pipeline = build_pipeline(tier="personal")
        assert [layer.name for layer in pipeline.layers] == ["identity", "global"]

    async def test_enterprise_pipeline_has_six_layers(self) -> None:
        pipeline = build_pipeline(tier="enterprise")
        assert [layer.name for layer in pipeline.layers] == [
            "identity",
            "global",
            "classification",
            "provider",
            "agent",
            "sandbox",
        ]

    async def test_federal_pipeline_has_seven_layers(self) -> None:
        pipeline = build_pipeline(tier="federal")
        assert [layer.name for layer in pipeline.layers] == [
            "identity",
            "global",
            "classification",
            "provider",
            "agent",
            "team",
            "sandbox",
        ]

    async def test_personal_allows_signed_call_by_default(self) -> None:
        ident = AgentIdentity.generate(org="test", agent_type="exec")
        pipeline = build_pipeline(tier="personal")
        result = await pipeline.evaluate(self._signed(ident), make_ctx("personal"))
        assert result.outcome == "allow"

    async def test_global_denylist_denies_even_when_signed(self) -> None:
        ident = AgentIdentity.generate(org="test", agent_type="exec")
        pipeline = build_pipeline(
            tier="personal",
            global_deny_rules={"read_file": "file access denied"},
        )
        result = await pipeline.evaluate(self._signed(ident, "read_file"), make_ctx("personal"))
        assert result.outcome == "deny"
        assert "global" in (result.layer or "")

    async def test_agent_allowlist_denies_unlisted_tool(self) -> None:
        ident = AgentIdentity.generate(org="test", agent_type="exec")
        pipeline = build_pipeline(
            tier="enterprise",
            agent_registry={ident.did: ident.public_key},
            agent_allowlists={ident.did: {"allowed_tool"}},
        )
        result = await pipeline.evaluate(
            self._signed(ident, "forbidden_tool"), make_ctx("enterprise")
        )
        assert result.outcome == "deny"
        assert result.layer == "agent"  # denied by allowlist, not identity

    async def test_agent_allowlist_permits_listed_tool(self) -> None:
        ident = AgentIdentity.generate(org="test", agent_type="exec")
        pipeline = build_pipeline(
            tier="enterprise",
            agent_registry={ident.did: ident.public_key},
            agent_allowlists={ident.did: {"allowed_tool"}},
        )
        result = await pipeline.evaluate(
            self._signed(ident, "allowed_tool"), make_ctx("enterprise")
        )
        assert result.outcome == "allow"


# ---------------------------------------------------------------------------
# SPEC-053 T-12 — authenticate BEFORE short-circuits; bind cache to identity
# (SPEC-034 review Findings 2 + 6)
# ---------------------------------------------------------------------------


class TestAuthenticateBeforeShortCircuits:
    def _ident(self) -> AgentIdentity:
        return AgentIdentity.generate(org="test", agent_type="exec")

    def _signed(self, ident: AgentIdentity, tool_name: str) -> ToolCall:
        return sign_call(make_call(tool_name=tool_name, agent_did=ident.did), ident)

    def _unsigned(self, ident: AgentIdentity, tool_name: str) -> ToolCall:
        return make_call(tool_name=tool_name, agent_did=ident.did)

    async def test_finding6_restricted_unsigned_safeset_call_denied(self) -> None:
        """Finding 6: in restricted mode, an UNSIGNED safe-set call must be
        authenticated (and denied) — the safe-set short-circuit no longer
        bypasses identity."""
        ident = self._ident()
        pipeline = build_pipeline(
            tier="personal",
            agent_registry={ident.did: ident.public_key},
            max_bundle_age_seconds=60.0,
            safe_set={"safe_tool"},
        )
        ctx = make_ctx(bundle_age=120.0)  # stale bundle → restricted mode active

        decision = await pipeline.evaluate(self._unsigned(ident, "safe_tool"), ctx)
        assert decision.is_deny()
        assert decision.rule_id == "identity.unsigned_or_invalid"

    async def test_finding6_restricted_signed_safeset_call_allows(self) -> None:
        ident = self._ident()
        pipeline = build_pipeline(
            tier="personal",
            agent_registry={ident.did: ident.public_key},
            max_bundle_age_seconds=60.0,
            safe_set={"safe_tool"},
        )
        ctx = make_ctx(bundle_age=120.0)
        decision = await pipeline.evaluate(self._signed(ident, "safe_tool"), ctx)
        assert decision.outcome == "allow"

    async def test_finding2a_unsigned_call_never_hits_signed_cache(self) -> None:
        """Finding 2a: a cached ALLOW for a signed call must NOT be served to an
        unsigned call with identical (tool_name, arguments, agent_did,
        classification). It gets no cache hit and is denied by identity."""
        ident = self._ident()
        pipeline = build_pipeline(
            tier="personal",
            agent_registry={ident.did: ident.public_key},
            cache_ttl_seconds=300.0,
        )
        ctx = make_ctx()

        allow = await pipeline.evaluate(self._signed(ident, "read_file"), ctx)
        assert allow.outcome == "allow"  # this decision is now cached

        # Same (tool_name, arguments, agent_did, classification) but UNSIGNED.
        replay = await pipeline.evaluate(self._unsigned(ident, "read_file"), ctx)
        assert replay.is_deny()
        assert replay.rule_id == "identity.unsigned_or_invalid"

    async def test_finding2b_deregistered_replay_within_ttl_denied(self) -> None:
        """Finding 2b: after an ALLOW is cached, de-registering the agent must
        cause its replayed (validly-signed) call to DENY within the TTL window —
        identity runs before the cache, so a stale ALLOW cannot be replayed."""
        ident = self._ident()
        registry = {ident.did: ident.public_key}
        pipeline = build_pipeline(
            tier="enterprise",
            agent_registry=registry,
            cache_ttl_seconds=300.0,
        )
        ctx = make_ctx(tier="enterprise")
        signed = self._signed(ident, "read_file")

        allow = await pipeline.evaluate(signed, ctx)
        assert allow.outcome == "allow"  # cached

        del registry[ident.did]  # revoke admission (same dict the layer holds)

        replay = await pipeline.evaluate(signed, ctx)
        assert replay.is_deny()
        assert replay.rule_id == "identity.not_admitted"
