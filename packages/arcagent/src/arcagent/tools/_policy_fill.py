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

# SPEC-039 OQ-3 — conservative per-tier default ceilings (SPEC-038 REQ-001/004/005).
# Applied only where the operator left a ceiling unset, so budgets are ON by
# default rather than operator-only. Federal is the tightest floor — a federal
# agent is never unbounded by omission (a missing budget block still caps it);
# enterprise gets a looser default cap; personal is absent here and stays
# unbounded/relaxable when unset. An explicit operator ceiling always wins.
_TIER_DEFAULT_MAX_TOKENS: dict[str, int] = {"federal": 500_000, "enterprise": 2_000_000}
_TIER_DEFAULT_MAX_COST_USD: dict[str, float] = {"federal": 10.0, "enterprise": 50.0}
_TIER_DEFAULT_MAX_REQUESTS: dict[str, int] = {"federal": 500, "enterprise": 2_000}


def _effective_ceilings(config: ArcAgentConfig) -> tuple[int | None, float | None, int | None]:
    """Resolve (tokens, cost, requests) ceilings after applying tier defaults.

    An operator-set value always wins; an unset value falls back to the tier
    default (``None`` for personal — unbounded). This is the single point where
    tier stringency turns the operator-optional budget into a default-on cap.
    """
    budget = config.budget
    tier = config.security.tier
    max_tokens = (
        budget.max_tokens if budget.max_tokens is not None else _TIER_DEFAULT_MAX_TOKENS.get(tier)
    )
    max_cost = (
        budget.max_cost_usd
        if budget.max_cost_usd is not None
        else _TIER_DEFAULT_MAX_COST_USD.get(tier)
    )
    max_requests = (
        budget.max_requests
        if budget.max_requests is not None
        else _TIER_DEFAULT_MAX_REQUESTS.get(tier)
    )
    return max_tokens, max_cost, max_requests


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

    Returns the effective ceilings threaded onto arcrun's RunState after the
    per-tier default fill. A ``None`` ceiling is unbounded (personal-relaxable
    default). The value is operator-authored config — no agent tool can raise
    it, so at enterprise/federal it acts as a non-relaxable floor.
    """
    max_tokens, max_cost, _ = _effective_ceilings(config)
    return max_tokens, max_cost


def resolve_provider_limits(config: ArcAgentConfig) -> dict[str, ProviderLimit]:
    """Build the ProviderLayer limit map keyed by the TRUSTED provider label (REQ-004).

    The label is the configured model identity (``llm.model``) — the same trusted
    value the dispatch fill attributes usage to (never ``response.model``). Unset
    ceilings become an unbounded sentinel so only the capped metric can trip
    ``provider.budget_exceeded``. Returns ``{}`` only when every effective ceiling
    is unbounded (personal with no operator budget), leaving ProviderLayer a
    no-op ALLOW; federal/enterprise get a default-on limit.
    """
    max_tokens, max_cost, max_requests = _effective_ceilings(config)
    if max_tokens is None and max_cost is None and max_requests is None:
        return {}
    return {
        config.llm.model: ProviderLimit(
            max_tokens=max_tokens if max_tokens is not None else _UNBOUNDED_INT,
            max_cost=max_cost if max_cost is not None else _UNBOUNDED_COST,
            max_requests=max_requests if max_requests is not None else _UNBOUNDED_INT,
        )
    }


__all__ = [
    "build_clearance_context",
    "build_provider_usage",
    "resolve_provider_limits",
    "resolve_run_budget",
]
