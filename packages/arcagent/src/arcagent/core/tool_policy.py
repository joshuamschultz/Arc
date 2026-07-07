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
    sign_call,
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


__all__ = [
    "AgentLayer",
    "AuditSink",
    "Decision",
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
    "sign_call",
]
