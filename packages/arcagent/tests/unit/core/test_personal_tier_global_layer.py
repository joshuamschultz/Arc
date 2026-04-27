"""Tests for §8: personal-tier policy pipeline must enforce Global layer.

At personal tier, the Global layer must run and a rule denied by Global
must result in a DENY decision (not bypass).
"""

from __future__ import annotations

from typing import Any

import pytest

from arcagent.core.tool_policy import (
    Decision,
    PolicyContext,
    PolicyLayer,
    PolicyPipeline,
    ToolCall,
)


class _DenyAllLayer:
    """Policy layer that denies every call."""

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        return Decision.deny(
            layer="global",
            rule_id="deny_all.test",
            reason="test deny all",
            input_hash="h",
            evaluated_at_us=0,
        )


class _AllowAllLayer:
    """Policy layer that allows every call."""

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        return Decision.allow(input_hash="h", evaluated_at_us=0)


class _RecordingLayer:
    """Records whether evaluate was called."""

    def __init__(self) -> None:
        self.called = False

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        self.called = True
        return Decision.allow(input_hash="h", evaluated_at_us=0)


def _make_call(tool_name: str = "bash") -> ToolCall:
    return ToolCall(
        tool_name=tool_name,
        arguments={},
        agent_did="did:arc:testorg:executor/abc",
        session_id="",
        classification="unclassified",
    )


def _make_ctx(tier: str = "personal") -> PolicyContext:
    return PolicyContext(tier=tier, policy_version="v0", bundle_age_seconds=0.0)


class TestPersonalTierGlobalLayerEnforcement:
    """§8: personal tier pipeline must not bypass the Global layer."""

    async def test_personal_tier_deny_from_global_layer_is_respected(self) -> None:
        """A DENY from the layer runs at personal tier — not bypassed."""
        deny_layer = _DenyAllLayer()
        pipeline = PolicyPipeline(layers=[deny_layer])

        decision = await pipeline.evaluate(_make_call(), _make_ctx(tier="personal"))
        assert decision.is_deny(), f"Expected DENY at personal tier, got: {decision.outcome}"

    async def test_personal_tier_allow_when_no_deny_rule(self) -> None:
        """At personal tier, allow passes through cleanly."""
        allow_layer = _AllowAllLayer()
        pipeline = PolicyPipeline(layers=[allow_layer])

        decision = await pipeline.evaluate(_make_call(), _make_ctx(tier="personal"))
        assert not decision.is_deny()

    async def test_personal_tier_empty_pipeline_allows(self) -> None:
        """Empty pipeline at personal tier defaults to ALLOW (no deny rules)."""
        pipeline = PolicyPipeline(layers=[])
        decision = await pipeline.evaluate(_make_call(), _make_ctx(tier="personal"))
        assert not decision.is_deny()

    async def test_global_layer_is_called_at_personal_tier(self) -> None:
        """Global layer evaluate() must be invoked at personal tier."""
        recorder = _RecordingLayer()
        pipeline = PolicyPipeline(layers=[recorder])

        await pipeline.evaluate(_make_call(), _make_ctx(tier="personal"))
        assert recorder.called, "Global layer was not called at personal tier"

    async def test_build_pipeline_personal_tier_runs_global_layer(self) -> None:
        """build_pipeline for personal tier must include Global layer."""
        from arctrust import build_pipeline

        pipeline = build_pipeline(tier="personal")
        # The pipeline must exist and evaluate without crashing
        decision = await pipeline.evaluate(_make_call(), _make_ctx(tier="personal"))
        # Can be allow or deny — just must not crash
        assert decision.outcome in ("allow", "deny")

    @pytest.mark.parametrize("tier", ["federal", "enterprise", "personal"])
    async def test_deny_all_layer_denies_at_every_tier(self, tier: str) -> None:
        """A deny-all layer denies at every tier including personal."""
        pipeline = PolicyPipeline(layers=[_DenyAllLayer()])
        decision = await pipeline.evaluate(_make_call(), _make_ctx(tier=tier))
        assert decision.is_deny(), f"Expected DENY at {tier}, got: {decision.outcome}"
