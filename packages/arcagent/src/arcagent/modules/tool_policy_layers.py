"""Superseded pre-SPEC-034 policy layer implementation — not wired into anything.

Nothing imports this module. The real, enforced policy engine
(``PolicyPipeline``, ``GlobalLayer``, ``ProviderLayer``, ``AgentLayer``,
``TeamLayer``, ``SandboxLayer``, ``ClassificationLayer``) lives in
``arctrust.policy`` and is re-exported for arcagent call sites via
``arcagent.core.tool_policy`` — that is what ``core/tool_registry.py`` and
``core/agent.py`` actually use. The types below (including the
``ProviderLayer``/``TeamLayer``/``SandboxLayer`` allow-everything stand-ins)
predate that consolidation and are kept only as historical reference.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from arcagent.core.errors import ArcAgentError

AuditSink = Callable[[str, dict[str, Any]], None]
MonotonicClock = Callable[[], float]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    """Immutable request to invoke a tool."""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    arguments: dict[str, Any]
    agent_did: str
    session_id: str
    classification: str
    parent_call_id: str | None = None


class PolicyContext(BaseModel):
    """Runtime context for policy evaluation — tier, bundle version, age."""

    model_config = ConfigDict(frozen=True)

    tier: Literal["federal", "enterprise", "personal"]
    policy_version: str
    bundle_age_seconds: float


class Decision(BaseModel):
    """Immutable result of a policy evaluation.

    ``outcome`` is authoritative. The remaining fields answer the three
    required questions when a call is denied: which layer, which rule,
    what inputs triggered it (stored in ``reason``).
    """

    model_config = ConfigDict(frozen=True)

    outcome: Literal["allow", "deny"]
    layer: str | None = None
    rule_id: str | None = None
    reason: str | None = None
    input_hash: str
    evaluated_at_us: int

    @classmethod
    def allow(cls, *, input_hash: str, evaluated_at_us: int) -> Decision:
        """Build an ALLOW decision."""
        return cls(
            outcome="allow",
            input_hash=input_hash,
            evaluated_at_us=evaluated_at_us,
        )

    @classmethod
    def deny(
        cls,
        *,
        layer: str,
        rule_id: str,
        reason: str,
        input_hash: str,
        evaluated_at_us: int,
    ) -> Decision:
        """Build a DENY decision with structured context."""
        return cls(
            outcome="deny",
            layer=layer,
            rule_id=rule_id,
            reason=reason,
            input_hash=input_hash,
            evaluated_at_us=evaluated_at_us,
        )

    def is_deny(self) -> bool:
        return self.outcome == "deny"


class PolicyDenied(ArcAgentError):  # noqa: N818 — domain convention
    """Raised by tool dispatch when the policy pipeline returns DENY."""

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
# Layer Protocol + concrete implementations
# ---------------------------------------------------------------------------


@runtime_checkable
class PolicyLayer(Protocol):
    """Single decision boundary within the pipeline."""

    name: str

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision: ...


class GlobalLayer:
    """Tenant-wide rules and forbidden capability compositions."""

    name = "global"

    def __init__(
        self,
        *,
        deny_rules: dict[str, str],
        forbidden_compositions: list[frozenset[str]],
    ) -> None:
        self._deny_rules = deny_rules
        self._forbidden_compositions = forbidden_compositions

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        reason = self._deny_rules.get(call.tool_name)
        now_us = _monotonic_us()
        if reason is not None:
            return Decision.deny(
                layer=self.name,
                rule_id="global.denylist",
                reason=reason,
                input_hash=_hash_call(call),
                evaluated_at_us=now_us,
            )
        return Decision.allow(input_hash=_hash_call(call), evaluated_at_us=now_us)


class ProviderLayer:
    """Unused stand-in — always allows.

    Superseded by ``arctrust.policy.ProviderLayer``, which is real.
    """

    name = "provider"

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        return Decision.allow(input_hash=_hash_call(call), evaluated_at_us=_monotonic_us())


class AgentLayer:
    """Per-agent allowlist enforcement."""

    name = "agent"

    def __init__(self, *, allowlist_by_agent: dict[str, set[str]]) -> None:
        self._allowlist = allowlist_by_agent

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        allow_set = self._allowlist.get(call.agent_did)
        now_us = _monotonic_us()
        if allow_set is not None and call.tool_name not in allow_set:
            return Decision.deny(
                layer=self.name,
                rule_id="agent.allowlist",
                reason=(
                    f"Tool {call.tool_name!r} not in agent allowlist for "
                    f"{call.agent_did}; agent has {sorted(allow_set)}"
                ),
                input_hash=_hash_call(call),
                evaluated_at_us=now_us,
            )
        return Decision.allow(input_hash=_hash_call(call), evaluated_at_us=now_us)


class TeamLayer:
    """Unused stand-in — always allows.

    Superseded by ``arctrust.policy.TeamLayer``, which is real.
    """

    name = "team"

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        return Decision.allow(input_hash=_hash_call(call), evaluated_at_us=_monotonic_us())


class SandboxLayer:
    """Unused stand-in — always allows.

    Superseded by ``arctrust.policy.SandboxLayer``, which is real.
    """

    name = "sandbox"

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        return Decision.allow(input_hash=_hash_call(call), evaluated_at_us=_monotonic_us())


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def _monotonic_us() -> int:
    """Return the current monotonic time in microseconds."""
    return int(time.monotonic() * 1_000_000)


def _hash_call(call: ToolCall) -> str:
    """Deterministic hash of the request payload for cache keys + audit."""
    payload = json.dumps(
        {
            "tool_name": call.tool_name,
            "arguments": call.arguments,
            "agent_did": call.agent_did,
            "classification": call.classification,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
