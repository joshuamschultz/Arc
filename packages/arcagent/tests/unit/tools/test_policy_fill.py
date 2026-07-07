"""SPEC-038 dispatch-fill helpers — budgets + clearance resolution (F1/F6)."""

from __future__ import annotations

from arctrust.classification import Classification
from arctrust.identity import AgentIdentity

from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    BudgetConfig,
    LLMConfig,
)
from arcagent.tools._policy_fill import (
    build_clearance_context,
    resolve_provider_limits,
    resolve_run_budget,
)


def _config(**budget_kw: object) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name="a", org="o", type="executor"),
        llm=LLMConfig(model="anthropic/claude"),
        budget=BudgetConfig(**budget_kw),  # type: ignore[arg-type]
    )


class TestRunBudget:
    def test_unset_is_unbounded(self) -> None:
        assert resolve_run_budget(_config()) == (None, None)

    def test_set_ceilings_returned(self) -> None:
        assert resolve_run_budget(_config(max_tokens=500, max_cost_usd=2.0)) == (500, 2.0)


class TestProviderLimits:
    def test_no_budget_yields_empty_map(self) -> None:
        assert resolve_provider_limits(_config()) == {}

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
