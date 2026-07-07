"""SPEC-038 — canonical classification ladder + ClassificationLayer + label fix."""

from __future__ import annotations

import pytest

from arctrust.classification import Classification, dominates, parse_classification
from arctrust.policy import (
    ClassificationLayer,
    ClearanceContext,
    PolicyContext,
    ProviderLayer,
    ProviderLimit,
    ProviderUsage,
    ToolCall,
)


def _call(tool: str = "read_secret") -> ToolCall:
    return ToolCall(
        tool_name=tool,
        arguments={},
        agent_did="did:arc:test:agent/abc",
        session_id="s1",
        classification="unclassified",
    )


def _ctx(**kw: object) -> PolicyContext:
    base: dict[str, object] = {
        "tier": "federal",
        "policy_version": "v1",
        "bundle_age_seconds": 0.0,
    }
    base.update(kw)
    return PolicyContext(**base)  # type: ignore[arg-type]


class TestLadder:
    def test_ordering(self) -> None:
        assert (
            Classification.UNCLASSIFIED
            < Classification.CUI
            < Classification.CONFIDENTIAL
            < Classification.SECRET
            < Classification.TOP_SECRET
        )

    def test_dominates(self) -> None:
        assert dominates(Classification.SECRET, Classification.CUI)
        assert not dominates(Classification.CUI, Classification.SECRET)
        assert dominates(Classification.SECRET, Classification.SECRET)

    def test_parse_strict_raises_on_unknown(self) -> None:
        with pytest.raises(ValueError):
            parse_classification("SECERT", strict=True)
        with pytest.raises(ValueError):
            parse_classification("", strict=True)

    def test_parse_lenient_defaults(self) -> None:
        assert parse_classification("SECERT", strict=False) == Classification.UNCLASSIFIED
        assert parse_classification("secret", strict=False) == Classification.SECRET


class TestClassificationLayer:
    async def test_read_up_denied(self) -> None:
        layer = ClassificationLayer()
        cc = ClearanceContext(
            caller_clearance=Classification.CUI,
            resource_classification=Classification.SECRET,
        )
        decision = await layer.evaluate(_call(), _ctx(clearance=cc))
        assert decision.is_deny()
        assert decision.rule_id == "classification.read_up"

    async def test_cleared_caller_allowed(self) -> None:
        layer = ClassificationLayer()
        cc = ClearanceContext(
            caller_clearance=Classification.SECRET,
            resource_classification=Classification.SECRET,
        )
        decision = await layer.evaluate(_call(), _ctx(clearance=cc))
        assert not decision.is_deny()

    async def test_missing_state_denied_when_enforced(self) -> None:
        layer = ClassificationLayer(enforced=True)
        decision = await layer.evaluate(_call(), _ctx())
        assert decision.is_deny()
        assert decision.rule_id == "classification.state_missing"

    async def test_missing_state_allowed_when_relaxable(self) -> None:
        layer = ClassificationLayer(enforced=True, relaxable=True)
        decision = await layer.evaluate(_call(), _ctx(tier="personal"))
        assert not decision.is_deny()

    async def test_missing_state_noop_when_unenforced(self) -> None:
        layer = ClassificationLayer()
        decision = await layer.evaluate(_call(), _ctx())
        assert not decision.is_deny()


class TestProviderUnknownLabel:
    async def test_unknown_label_denies_when_not_relaxable(self) -> None:
        layer = ProviderLayer(
            limits_by_provider={"anthropic": ProviderLimit(max_tokens=100, max_cost=1.0, max_requests=10)}
        )
        usage = ProviderUsage(provider="bogus", tokens_used=1, cost_used=0.0, requests_in_window=1)
        decision = await layer.evaluate(_call(), _ctx(provider_usage=usage))
        assert decision.is_deny()
        assert decision.rule_id == "provider.unknown_label"

    async def test_unknown_label_allowed_when_relaxable(self) -> None:
        layer = ProviderLayer(
            limits_by_provider={"anthropic": ProviderLimit(max_tokens=100, max_cost=1.0, max_requests=10)},
            relaxable=True,
        )
        usage = ProviderUsage(provider="bogus", tokens_used=1, cost_used=0.0, requests_in_window=1)
        decision = await layer.evaluate(_call(), _ctx(provider_usage=usage))
        assert not decision.is_deny()


class TestFederalAutoFailClosed:
    """SPEC-038 F5 — federal forces classification enforcement from the tier
    alone; it never depends on the operator flag. personal is the only relaxable
    tier, enterprise honors the flag."""

    async def _missing_clearance_decision(self, tier: str, *, enforced: bool):  # type: ignore[no-untyped-def]
        from arctrust.identity import AgentIdentity
        from arctrust.policy import build_pipeline, sign_call

        identity = AgentIdentity.generate("org", "agent")
        pipeline = build_pipeline(
            tier=tier,  # type: ignore[arg-type]
            agent_registry={identity.did: identity.public_key},
            classification_enforced=enforced,
        )
        call = sign_call(
            ToolCall(
                tool_name="read_secret",
                arguments={},
                agent_did=identity.did,
                session_id="s1",
                classification="unclassified",
            ),
            identity,
        )
        ctx = PolicyContext(tier=tier, policy_version="v1", bundle_age_seconds=0.0)  # type: ignore[arg-type]
        return await pipeline.evaluate(call, ctx)

    async def test_federal_denies_missing_clearance_even_with_flag_off(self) -> None:
        decision = await self._missing_clearance_decision("federal", enforced=False)
        assert decision.is_deny()
        assert decision.rule_id == "classification.state_missing"

    async def test_enterprise_flag_off_allows_missing_clearance(self) -> None:
        decision = await self._missing_clearance_decision("enterprise", enforced=False)
        assert not decision.is_deny()

    async def test_enterprise_flag_on_denies_missing_clearance(self) -> None:
        decision = await self._missing_clearance_decision("enterprise", enforced=True)
        assert decision.is_deny()
        assert decision.rule_id == "classification.state_missing"
