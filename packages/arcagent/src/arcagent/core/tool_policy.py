"""Tool Policy Pipeline — arcagent integration layer.

The policy ENGINE (pipeline, decision, layers, tier config) lives in
``arctrust.policy``. This module:

1. Re-exports engine types so arcagent call sites import from one place.
2. Defines ``PolicyDenied`` — the exception raised when dispatch is denied.
   This must live in arcagent because it inherits from ``ArcAgentError``.

Forbidden-composition enforcement (the lethal-trifecta subset test) is LIVE in
arctrust's ``GlobalLayer`` (SPEC-035) — arcagent maps tool tags to trifecta legs
(``core.session_internal.capability_ledger``) and hands arctrust resolved
frozensets. There is no arcagent copy of the checker.

arcagent does NOT reimplement the pipeline engine. It imports and reuses the
canonical implementation from arctrust.

The ``ToolPolicyPipeline`` alias has been removed. Use ``PolicyPipeline`` directly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum

# Re-export engine types — arcagent consumers import from tool_policy, not arctrust directly.
from arctrust.policy import (
    AgentLayer,
    AuditSink,
    ClassificationLayer,
    ClearanceContext,
    Decision,
    GlobalLayer,
    PolicyContext,
    PolicyLayer,
    PolicyPipeline,
    ProviderLayer,
    ProviderUsage,
    SandboxLayer,
    TeamLayer,
    ToolCall,
    build_pipeline,
    sign_call,
)
from pydantic import BaseModel, ConfigDict

from arcagent.core.errors import ArcAgentError

_logger = logging.getLogger("arcagent.tool_policy")

# Re-export MonotonicClock for type-checking compatibility.
MonotonicClock = Callable[[], float]


# ---------------------------------------------------------------------------
# PolicyDenied — raised by tool dispatch when the pipeline returns DENY
# ---------------------------------------------------------------------------


class PolicyDenied(ArcAgentError):  # noqa: N818 — domain convention
    """Raised by tool dispatch when the policy pipeline returns DENY.

    Carries the full :class:`Decision` so callers and auditors can see
    which layer denied, which rule matched, and why.
    """

    _component = "tool_policy"

    def __init__(self, decision: Decision) -> None:
        reason = decision.reason or "denied"
        layer = decision.layer or "pipeline"
        rule = decision.rule_id or "unknown"
        message = f"[{layer}:{rule}] {reason}"
        super().__init__(
            code="POLICY_DENIED",
            message=message,
            details={
                "layer": decision.layer,
                "rule_id": decision.rule_id,
                "reason": decision.reason,
            },
        )
        self.decision = decision


# ---------------------------------------------------------------------------
# ToolPolicySummary — the ONE interpreter of an allow-gated policy surface
# ---------------------------------------------------------------------------
#
# H-010: arcui's Identity tab and Tools tab each re-derived a verdict from a
# raw ``allow`` list and disagreed — one read an empty list as "nothing
# configured" (allow-all), the other as "configured empty" (deny-all). Both
# readings are legitimate outcomes; the bug was computing the verdict twice
# instead of once. This is the once.
#
# The distinction already exists, correctly, in ``AgentLayer.evaluate``
# above: ``self._allowlist.get(call.agent_did)`` returns ``None`` when the
# agent has no entry (unconstrained — every tool passes) versus an empty
# ``set()`` when the agent has an entry that names no tools (every tool
# fails ``tool_name not in allow_set``). ``summarize_tool_policy`` names that
# same None-vs-empty distinction so any allow-gated surface — a Global-scope
# config table, a per-agent allowlist map lookup, or any future layer — can
# report it consistently instead of re-deriving it ad hoc.


class ToolPolicyState(StrEnum):
    """The three postures an allow-gated policy surface can be in.

    Mirrors the project's configured-gate fail-closed invariant (SPEC-034):
    a surface with NO allowlist configured is a no-op (everything passes,
    matching ``AgentLayer``'s ``allow_set is None`` branch); one WITH an
    allowlist that happens to be empty is configured-but-blind and denies
    everything it gates (``allow_set == set()``); one with entries gates by
    membership.
    """

    DEFAULT_ALLOW = "default-allow"
    DENY_ALL = "deny-all"
    EXPLICIT = "explicit"


class ToolPolicySummary(BaseModel):
    """One authoritative read of an allow/deny gate.

    Both arcui's Identity tab and Tools tab render their headline label from
    this model's ``label`` field — never from a locally re-derived verdict —
    so the two surfaces cannot disagree about what an agent's tool policy
    means (H-010).
    """

    model_config = ConfigDict(frozen=True)

    state: ToolPolicyState
    allow: list[str]
    deny: list[str]
    label: str


def summarize_tool_policy(
    allow: list[str] | None, deny: list[str] | None = None
) -> ToolPolicySummary:
    """Interpret an allow/deny gate with configured-gate fail-closed semantics.

    ``allow=None`` means no allowlist is configured for this gate — e.g. an
    ``[tools.policy]`` TOML table that omits the ``allow`` key, or
    ``AgentLayer``'s ``allowlist_by_agent.get(agent_did)`` finding no entry.
    That is a no-op: every tool passes (``DEFAULT_ALLOW``).

    ``allow=[]`` means the gate carries an allowlist that was configured and
    is empty — the operator (or an agent-scoped layer) named the gate
    explicitly and named zero tools. Every tool it gates is denied
    (``DENY_ALL``), exactly as ``AgentLayer`` denies when ``tool_name not in
    allow_set`` for an empty ``allow_set``.

    A populated ``allow`` gates by membership (``EXPLICIT``). ``deny`` never
    changes the state — it is an orthogonal subtraction layered on top — but
    rides along on the summary so callers render one label instead of
    stitching two lists back together.
    """
    deny_list = list(deny or [])
    if allow is None:
        return ToolPolicySummary(
            state=ToolPolicyState.DEFAULT_ALLOW,
            allow=[],
            deny=deny_list,
            label=_with_deny_suffix("allow-all", deny_list),
        )
    if len(allow) == 0:
        return ToolPolicySummary(
            state=ToolPolicyState.DENY_ALL,
            allow=[],
            deny=deny_list,
            label="deny-all",
        )
    allow_list = list(allow)
    return ToolPolicySummary(
        state=ToolPolicyState.EXPLICIT,
        allow=allow_list,
        deny=deny_list,
        label=_with_deny_suffix(f"allow {len(allow_list)}", deny_list),
    )


def _with_deny_suffix(label: str, deny_list: list[str]) -> str:
    """Append a deny count to ``label`` when a denylist is also configured."""
    if not deny_list:
        return label
    return f"{label} (deny {len(deny_list)})"


__all__ = [
    "AgentLayer",
    "AuditSink",
    "ClassificationLayer",
    "ClearanceContext",
    "Decision",
    "GlobalLayer",
    "MonotonicClock",
    "PolicyContext",
    "PolicyDenied",
    "PolicyLayer",
    "PolicyPipeline",
    "ProviderLayer",
    "ProviderUsage",
    "SandboxLayer",
    "TeamLayer",
    "ToolCall",
    "ToolPolicyState",
    "ToolPolicySummary",
    "build_pipeline",
    "sign_call",
    "summarize_tool_policy",
]
