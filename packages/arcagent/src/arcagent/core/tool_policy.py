"""Tool Policy Pipeline — arcagent integration layer.

The policy ENGINE (pipeline, decision, layers, tier config) lives in
``arctrust.policy``. This module:

1. Re-exports engine types so arcagent call sites import from one place.
2. Defines ``PolicyDenied`` — the exception raised when dispatch is denied.
   This must live in arcagent because it inherits from ``ArcAgentError``.
3. Defines ``ForbiddenCompositionChecker`` — used by tool_registry to reject
   batches whose combined capability tags form a forbidden set (arXiv:2603.15973).

arcagent does NOT reimplement the pipeline engine. It imports and reuses the
canonical implementation from arctrust.

The ``ToolPolicyPipeline`` alias has been removed. Use ``PolicyPipeline`` directly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

# Re-export engine types — arcagent consumers import from tool_policy, not arctrust directly.
from arctrust.policy import (
    AgentLayer,
    AuditSink,
    Decision,
    GlobalLayer,
    PolicyContext,
    PolicyLayer,
    PolicyPipeline,
    ProviderLayer,
    SandboxLayer,
    TeamLayer,
    TierConfig,
    ToolCall,
    build_pipeline,
)

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
# ForbiddenCompositionChecker — non-compositional safety (arXiv:2603.15973)
# ---------------------------------------------------------------------------


class ForbiddenCompositionChecker:
    """Reject tool batches whose combined capabilities are forbidden.

    Two individually-safe tools can compose into a forbidden outcome
    (arXiv:2603.15973). Example: ``file_read + network_egress = exfiltration``.

    Each tool declares ``capability_tags``; at batch dispatch time we union
    those tags and check whether any forbidden set is a subset of the union.
    Forbidden sets are declared at deployment time — audit-visible.
    """

    def __init__(self, *, forbidden: list[frozenset[str]]) -> None:
        self._forbidden = list(forbidden)

    def is_forbidden(self, capabilities: set[str]) -> bool:
        """True if ``capabilities`` contains any forbidden combination."""
        return self.first_forbidden(capabilities) is not None

    def first_forbidden(self, capabilities: set[str]) -> frozenset[str] | None:
        """Return the first matching forbidden set, or None.

        The returned set identifies WHY the batch was rejected — useful for
        structured deny reasons in the audit trail.
        """
        for combo in self._forbidden:
            if combo.issubset(capabilities):
                return combo
        return None


__all__ = [
    "AgentLayer",
    "AuditSink",
    "Decision",
    "ForbiddenCompositionChecker",
    "GlobalLayer",
    "MonotonicClock",
    "PolicyContext",
    "PolicyDenied",
    "PolicyLayer",
    "PolicyPipeline",
    "ProviderLayer",
    "SandboxLayer",
    "TeamLayer",
    "TierConfig",
    "ToolCall",
    "build_pipeline",
]
