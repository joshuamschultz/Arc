"""SPEC-038 dispatch-fill helpers — budgets + clearance resolution (F1/F6)."""

from __future__ import annotations

from arctrust.classification import Classification
from arctrust.identity import AgentIdentity

from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    BudgetConfig,
    LLMConfig,
    SecurityConfig,
)
from arcagent.tools._policy_fill import (
    _TIER_DEFAULT_MAX_COST_USD,
    _TIER_DEFAULT_MAX_TOKENS,
    build_clearance_context,
    resolve_provider_limits,
    resolve_run_budget,
)


def _config(*, tier: str = "personal", **budget_kw: object) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name="a", org="o", type="executor"),
        llm=LLMConfig(model="anthropic/claude"),
        security=SecurityConfig(tier=tier),
        budget=BudgetConfig(**budget_kw),  # type: ignore[arg-type]
    )


class TestRunBudget:
    def test_personal_unset_is_unbounded(self) -> None:
        # Personal stays relaxable/unbounded when the operator sets nothing.
        assert resolve_run_budget(_config(tier="personal")) == (None, None)

    def test_set_ceilings_returned(self) -> None:
        assert resolve_run_budget(_config(max_tokens=500, max_cost_usd=2.0)) == (500, 2.0)

    def test_federal_unset_has_enforced_default_ceiling(self) -> None:
        # SPEC-039 OQ-3 — a federal agent with no explicit budget is never
        # unbounded by omission: the conservative default floor applies.
        max_tokens, max_cost = resolve_run_budget(_config(tier="federal"))
        assert max_tokens == _TIER_DEFAULT_MAX_TOKENS["federal"]
        assert max_cost == _TIER_DEFAULT_MAX_COST_USD["federal"]

    def test_enterprise_unset_has_default_ceiling(self) -> None:
        max_tokens, max_cost = resolve_run_budget(_config(tier="enterprise"))
        assert max_tokens == _TIER_DEFAULT_MAX_TOKENS["enterprise"]
        assert max_cost == _TIER_DEFAULT_MAX_COST_USD["enterprise"]

    def test_operator_value_overrides_tier_default(self) -> None:
        # An explicit operator ceiling wins over the tier default; the unset
        # metric still falls back to the tier default.
        max_tokens, max_cost = resolve_run_budget(_config(tier="federal", max_tokens=123))
        assert max_tokens == 123
        assert max_cost == _TIER_DEFAULT_MAX_COST_USD["federal"]


class TestProviderLimits:
    def test_personal_no_budget_yields_empty_map(self) -> None:
        assert resolve_provider_limits(_config(tier="personal")) == {}

    def test_federal_no_budget_still_enforces_provider_limit(self) -> None:
        # The ProviderLayer must be ON by default at federal even with no
        # operator budget block, keyed by the trusted model label.
        limits = resolve_provider_limits(_config(tier="federal"))
        assert set(limits) == {"anthropic/claude"}
        assert limits["anthropic/claude"].max_tokens == _TIER_DEFAULT_MAX_TOKENS["federal"]

    def test_limit_keyed_by_trusted_model_label(self) -> None:
        limits = resolve_provider_limits(_config(max_tokens=100))
        assert set(limits) == {"anthropic/claude"}
        # Unset cost/request ceilings become unbounded sentinels — only the
        # metric the operator capped can trip the ProviderLayer.
        limit = limits["anthropic/claude"]
        assert limit.max_tokens == 100
        assert limit.max_cost == float("inf")


class TestClearanceContext:
    def test_unlabeled_tool_resolves_unclassified_not_none(self) -> None:
        # F6 — an unlisted tool must NOT no-op: it resolves to an UNCLASSIFIED
        # resource so the layer is a live gate (and federal does not brick).
        identity = AgentIdentity.generate("o", "a")
        identity.clearance = Classification.SECRET
        cc = build_clearance_context(identity, None, strict=False)
        assert cc is not None
        assert cc.caller_clearance == Classification.SECRET
        assert cc.resource_classification == Classification.UNCLASSIFIED

    def test_labeled_tool_uses_declared_classification(self) -> None:
        identity = AgentIdentity.generate("o", "a")
        cc = build_clearance_context(identity, "SECRET", strict=False)
        assert cc is not None
        assert cc.resource_classification == Classification.SECRET

    def test_none_identity_returns_none(self) -> None:
        assert build_clearance_context(None, "SECRET", strict=False) is None
