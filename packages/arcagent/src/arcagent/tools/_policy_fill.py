"""SPEC-038 dispatch fill helpers — bridge run/identity state onto policy types.

Pure functions that translate the live arcrun ``RunState`` and the agent
identity into the injected state the arctrust ProviderLayer / ClassificationLayer
compare against. Kept out of ``core/`` so the nucleus stays lean: these are
adapters between siblings, not registry internals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from arctrust import AgentIdentity
from arctrust.classification import Classification, parse_classification
from arctrust.policy import ClearanceContext, ProviderLimit, ProviderUsage

if TYPE_CHECKING:
    from arcagent.core.config import ArcAgentConfig

# Sentinel "no ceiling" values so a partially-configured budget (e.g. a token
# ceiling but no cost/request cap) only bites on the metric the operator set.
_UNBOUNDED_INT = 2**63 - 1
_UNBOUNDED_COST = float("inf")


def build_provider_usage(parent_state: Any, provider_label: str | None) -> ProviderUsage | None:
    """Bridge the live arcrun RunState onto a ProviderUsage (SPEC-038 REQ-004/010).

    The ``provider`` label is the TRUSTED, config-sourced identity of the model
    — never the attacker-suppliable ``LLMResponse.model``. Returns ``None`` when
    no run state is present (standalone/test dispatch) so the ProviderLayer
    applies its own fail-closed-above-personal rule.
    """
    if parent_state is None or provider_label is None:
        return None
    return ProviderUsage(
        provider=provider_label,
        tokens_used=parent_state.tokens_used["total"],
        cost_used=parent_state.cost_usd,
        requests_in_window=parent_state.tool_calls_made,
    )


def build_clearance_context(
    identity: AgentIdentity | None,
    resource_label: str | None,
    strict: bool,
) -> ClearanceContext | None:
    """Build the no-read-up labels for the ClassificationLayer (SPEC-038 REQ-023/F6).

    The caller clearance is the agent identity's; the resource classification is
    the per-tool config label, defaulting to ``UNCLASSIFIED`` when the tool is
    unlabeled. Defaulting (rather than returning ``None`` for unlabeled tools) is
    what makes the layer a live, honest gate at every tier — an unlisted tool no
    longer silently no-ops, and federal's forced enforcement does not brick every
    unclassified call. Returns ``None`` only when there is no identity (the
    IdentityLayer already denies that upstream). At federal ``strict`` makes an
    UNKNOWN label raise here — fail closed before evaluation (REQ-026).
    """
    if identity is None:
        return None
    resource = (
        parse_classification(resource_label, strict=strict)
        if resource_label is not None
        else Classification.UNCLASSIFIED
    )
    return ClearanceContext(
        caller_clearance=identity.clearance,
        resource_classification=resource,
    )


def resolve_run_budget(config: ArcAgentConfig) -> tuple[int | None, float | None]:
    """Resolve the per-run token + cost ceilings for the circuit-breaker (REQ-001/005).

    Returns the operator-configured ceilings threaded onto arcrun's RunState. A
    ``None`` ceiling is unbounded (personal-relaxable default). The value is
    operator-authored config — no agent tool can raise it, so at enterprise/
    federal a set ceiling is a non-relaxable floor.
    """
    budget = config.budget
    return budget.max_tokens, budget.max_cost_usd


def resolve_provider_limits(config: ArcAgentConfig) -> dict[str, ProviderLimit]:
    """Build the ProviderLayer limit map keyed by the TRUSTED provider label (REQ-004).

    The label is the configured model identity (``llm.model``) — the same trusted
    value the dispatch fill attributes usage to (never ``response.model``). Unset
    ceilings become an unbounded sentinel so only the metric the operator capped
    can trip ``provider.budget_exceeded``. Returns ``{}`` when no ceiling is set,
    leaving ProviderLayer a no-op ALLOW.
    """
    budget = config.budget
    if budget.max_tokens is None and budget.max_cost_usd is None:
        return {}
    return {
        config.llm.model: ProviderLimit(
            max_tokens=budget.max_tokens if budget.max_tokens is not None else _UNBOUNDED_INT,
            max_cost=budget.max_cost_usd if budget.max_cost_usd is not None else _UNBOUNDED_COST,
            max_requests=(
                budget.max_requests if budget.max_requests is not None else _UNBOUNDED_INT
            ),
        )
    }


__all__ = [
    "build_clearance_context",
    "build_provider_usage",
    "resolve_provider_limits",
    "resolve_run_budget",
]
