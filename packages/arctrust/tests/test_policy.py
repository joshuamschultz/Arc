"""Tests for arctrust.policy — pipeline, decision, layer protocol, tier config."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from arctrust.policy import (
    Decision,
    PolicyContext,
    PolicyLayer,
    PolicyPipeline,
    TierConfig,
    ToolCall,
    build_pipeline,
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
    return PolicyContext(
        tier=tier,  # type: ignore[arg-type]
        policy_version="1.0",
        bundle_age_seconds=bundle_age,
    )


class AllowLayer:
    name = "allow_all"

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        return Decision.allow(
            input_hash="abc", evaluated_at_us=int(time.monotonic() * 1_000_000)
        )


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
        with pytest.raises(Exception):
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

            async def evaluate(
                self, call: ToolCall, ctx: PolicyContext
            ) -> Decision:
                call_order.append(self.name)
                return Decision.allow(
                    input_hash="h",
                    evaluated_at_us=int(time.monotonic() * 1_000_000),
                )

        class TrackingDeny:
            name = "tracking_deny"

            async def evaluate(
                self, call: ToolCall, ctx: PolicyContext
            ) -> Decision:
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

        pipeline = PolicyPipeline(
            layers=[TrackingAllow(), TrackingDeny(), layer_after]
        )
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

            async def evaluate(
                self, call: ToolCall, ctx: PolicyContext
            ) -> Decision:
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

            async def evaluate(
                self, call: ToolCall, ctx: PolicyContext
            ) -> Decision:
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
    async def test_personal_pipeline_has_one_layer(self) -> None:
        pipeline = build_pipeline(tier="personal")
        assert len(pipeline.layers) == 1

    async def test_enterprise_pipeline_has_four_layers(self) -> None:
        pipeline = build_pipeline(tier="enterprise")
        assert len(pipeline.layers) == 4

    async def test_federal_pipeline_has_five_layers(self) -> None:
        pipeline = build_pipeline(tier="federal")
        assert len(pipeline.layers) == 5

    async def test_personal_allows_by_default(self) -> None:
        pipeline = build_pipeline(tier="personal")
        result = await pipeline.evaluate(make_call(), make_ctx("personal"))
        assert result.outcome == "allow"

    async def test_global_denylist_denies(self) -> None:
        pipeline = build_pipeline(
            tier="personal",
            global_deny_rules={"read_file": "file access denied"},
        )
        result = await pipeline.evaluate(
            make_call(tool_name="read_file"), make_ctx("personal")
        )
        assert result.outcome == "deny"
        assert "global" in (result.layer or "")

    async def test_agent_allowlist_denies_unlisted_tool(self) -> None:
        pipeline = build_pipeline(
            tier="enterprise",
            agent_allowlists={
                "did:arc:test:exec/aabbccdd": {"allowed_tool"},
            },
        )
        result = await pipeline.evaluate(
            make_call(
                tool_name="forbidden_tool",
                agent_did="did:arc:test:exec/aabbccdd",
            ),
            make_ctx("enterprise"),
        )
        assert result.outcome == "deny"

    async def test_agent_allowlist_permits_listed_tool(self) -> None:
        pipeline = build_pipeline(
            tier="enterprise",
            agent_allowlists={
                "did:arc:test:exec/aabbccdd": {"allowed_tool"},
            },
        )
        result = await pipeline.evaluate(
            make_call(
                tool_name="allowed_tool",
                agent_did="did:arc:test:exec/aabbccdd",
            ),
            make_ctx("enterprise"),
        )
        assert result.outcome == "allow"
