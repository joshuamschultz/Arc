"""Tool Policy Pipeline — layered, first-DENY-wins, fail-closed evaluator.

This module provides the policy ENGINE shared across all Arc packages. It
does NOT contain agent-specific layer implementations (those live in
arcagent.core.tool_policy). What lives here:

  - ToolCall / PolicyContext / Decision: the contract types
  - PolicyLayer: the Protocol every layer must satisfy
  - PolicyPipeline: the ordered, short-circuiting, fail-closed evaluator
  - TierConfig: deployment tier metadata consumed by build_pipeline
  - build_pipeline(): factory that assembles the correct layer set per tier
  - Concrete layers: GlobalLayer, ProviderLayer, AgentLayer, TeamLayer,
    SandboxLayer — kept here so build_pipeline can assemble them without
    importing arcagent.

SPEC-017 R-010 through R-018:
- R-010: 5-layer ordering (Global → Provider → Agent → Team → Sandbox)
- R-011: First-DENY-wins, structured deny reasons
- R-012: Exception → DENY (fail-closed)
- R-013: LRU cache keyed on (agent_did, tool_name, classification)
- R-014: Structured deny reasons (which layer, which rule, what inputs)
- R-017: Shadow mode — evaluate and log but always return ALLOW
- R-018: Restricted mode — stale bundle → deny all except safe_set

Tier matrix (R-010):
  federal:    Global + Provider + Agent + Team + Sandbox (5 layers)
  enterprise: Global + Provider + Agent + Sandbox        (4 layers)
  personal:   Global                                     (1 layer)
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

_logger = logging.getLogger("arctrust.policy")

# Type aliases for injected dependencies
AuditSink = Callable[[str, dict[str, Any]], None]
MonotonicClock = Callable[[], float]

_Tier = Literal["federal", "enterprise", "personal"]


# ---------------------------------------------------------------------------
# Contract types
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    """Immutable request to invoke a tool.

    Carries agent identity, classification context, and optional delegation
    lineage for classification propagation verification.
    """

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

    tier: _Tier
    policy_version: str
    bundle_age_seconds: float


class Decision(BaseModel):
    """Immutable result of a policy evaluation.

    ``outcome`` is the authoritative field. Deny decisions carry the three
    structured answers required by R-014: which layer, which rule, what inputs.
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
        """Build a DENY decision with full structured context (R-014)."""
        return cls(
            outcome="deny",
            layer=layer,
            rule_id=rule_id,
            reason=reason,
            input_hash=input_hash,
            evaluated_at_us=evaluated_at_us,
        )

    def is_deny(self) -> bool:
        """True when the outcome is deny."""
        return self.outcome == "deny"


# ---------------------------------------------------------------------------
# PolicyLayer Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class PolicyLayer(Protocol):
    """Single decision boundary within the pipeline.

    Each layer must be stateless with respect to a single evaluation.
    Internal caches and indexes are acceptable but must not mutate across
    calls. Layers MUST NOT raise — any exception is caught by the pipeline
    and converted to DENY.
    """

    name: str

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision: ...


# ---------------------------------------------------------------------------
# Concrete layer implementations
# ---------------------------------------------------------------------------


class GlobalLayer:
    """Tenant-wide denylist. O(1) lookup; no shared mutable state."""

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
        now_us = _now_us()
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
    """LLM provider budget and rate-limit gates. Stubbed — Phase 3 wiring.

    Default behavior is ALLOW so the pipeline produces meaningful results
    without a full provider stack.
    """

    name = "provider"

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        return Decision.allow(
            input_hash=_hash_call(call), evaluated_at_us=_now_us()
        )


class AgentLayer:
    """Per-agent allowlist enforcement.

    Agents in ``allowlist_by_agent`` may only call listed tools. Agents
    NOT in the map are unconstrained at this layer.
    """

    name = "agent"

    def __init__(self, *, allowlist_by_agent: dict[str, set[str]]) -> None:
        self._allowlist = allowlist_by_agent

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        allow_set = self._allowlist.get(call.agent_did)
        now_us = _now_us()
        if allow_set is not None and call.tool_name not in allow_set:
            return Decision.deny(
                layer=self.name,
                rule_id="agent.allowlist",
                reason=(
                    f"Tool {call.tool_name!r} not in agent allowlist for "
                    f"{call.agent_did}; allowed: {sorted(allow_set)}"
                ),
                input_hash=_hash_call(call),
                evaluated_at_us=now_us,
            )
        return Decision.allow(input_hash=_hash_call(call), evaluated_at_us=now_us)


class TeamLayer:
    """Team-scoped delegation rules. Stubbed — Phase 6 wiring."""

    name = "team"

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        return Decision.allow(
            input_hash=_hash_call(call), evaluated_at_us=_now_us()
        )


class SandboxLayer:
    """Dynamic-tool runtime constraints. Stubbed — Phase 7 wiring."""

    name = "sandbox"

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        return Decision.allow(
            input_hash=_hash_call(call), evaluated_at_us=_now_us()
        )


# ---------------------------------------------------------------------------
# TierConfig — deployment tier metadata
# ---------------------------------------------------------------------------


class TierConfig(BaseModel):
    """Deployment tier configuration.

    Controls which pipeline layers are active and what resource limits apply
    for that tier. Consumers call ``TierConfig.for_tier(tier_name)`` to get
    the correct config.
    """

    model_config = ConfigDict(frozen=True)

    tier: _Tier
    max_parallel_tools: int
    """Maximum concurrent tool executions. Federal caps HTTPS tools at 4 (R-025)."""

    layer_names: tuple[str, ...]
    """Ordered layer names active for this tier."""

    @classmethod
    def for_tier(cls, tier: _Tier) -> TierConfig:
        """Return the TierConfig for a deployment tier.

        Raises:
            ValueError: tier is not one of federal/enterprise/personal.
        """
        configs: dict[str, TierConfig] = {
            "federal": cls(
                tier="federal",
                max_parallel_tools=4,  # R-025: FIPS cap
                layer_names=("global", "provider", "agent", "team", "sandbox"),
            ),
            "enterprise": cls(
                tier="enterprise",
                max_parallel_tools=10,
                layer_names=("global", "provider", "agent", "sandbox"),
            ),
            "personal": cls(
                tier="personal",
                max_parallel_tools=10,
                layer_names=("global",),
            ),
        }
        if tier not in configs:
            raise ValueError(
                f"Unknown tier {tier!r}. Must be one of: {list(configs.keys())}"
            )
        return configs[tier]


# ---------------------------------------------------------------------------
# PolicyPipeline
# ---------------------------------------------------------------------------


class PolicyPipeline:
    """Ordered, short-circuiting, fail-closed policy evaluator.

    Parameters
    ----------
    layers:
        Ordered list of PolicyLayer objects. First-DENY-wins.
    cache_ttl_seconds:
        How long a decision is reused, keyed on
        (agent_did, tool_name, classification, input_hash). 0 disables.
    max_bundle_age_seconds:
        If the policy bundle is older than this, enter restricted mode —
        only tools in ``safe_set`` are permitted. None disables this check.
    safe_set:
        Tools allowed in restricted mode. Ignored when max_bundle_age is None.
    shadow:
        When True, evaluate normally but always return ALLOW.
    audit_sink:
        Callback invoked once per evaluation with (event_type, payload).
        Never raises; any exception is swallowed.
    monotonic:
        Injection seam for time.monotonic — tests pass a fake clock.
    """

    def __init__(
        self,
        layers: list[PolicyLayer],
        *,
        cache_ttl_seconds: float = 0.0,
        max_bundle_age_seconds: float | None = None,
        safe_set: set[str] | None = None,
        shadow: bool = False,
        audit_sink: AuditSink | None = None,
        monotonic: MonotonicClock | None = None,
    ) -> None:
        self._layers = list(layers)
        self._cache_ttl = cache_ttl_seconds
        self._max_bundle_age = max_bundle_age_seconds
        self._safe_set = safe_set or set()
        self._shadow = shadow
        self._audit_sink = audit_sink
        self._monotonic = monotonic or time.monotonic
        # OrderedDict used as an LRU-style cache; oldest entries evicted first.
        self._cache: OrderedDict[str, tuple[Decision, float]] = OrderedDict()
        self._cache_max = 10_000

    @property
    def layers(self) -> list[PolicyLayer]:
        """Expose layers for introspection."""
        return list(self._layers)

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        """Run layered evaluation. First DENY wins. Exceptions are DENY."""
        started_at = self._monotonic()

        # Restricted mode pre-check (stale bundle + offline)
        restricted = self._check_restricted(call, ctx)
        if restricted is not None:
            self._emit_audit(call, ctx, restricted, started_at)
            return self._shadow_override(restricted)

        # Cache hit
        cache_key = self._cache_key(call)
        cached = self._cache_get(cache_key)
        if cached is not None:
            self._emit_audit(call, ctx, cached, started_at, cache_hit=True)
            return self._shadow_override(cached)

        # Layered evaluation — first DENY wins, exception → DENY (R-012)
        decision: Decision | None = None
        for layer in self._layers:
            try:
                decision = await layer.evaluate(call, ctx)
            except Exception as exc:
                _logger.exception(
                    "Policy layer %r raised — failing closed (R-012)", layer.name
                )
                decision = Decision.deny(
                    layer=layer.name,
                    rule_id="layer_error",
                    reason=f"{type(exc).__name__}: {exc}",
                    input_hash=_hash_call(call),
                    evaluated_at_us=_now_us(),
                )
                break
            if decision.is_deny():
                break

        if decision is None:
            decision = Decision.allow(
                input_hash=_hash_call(call), evaluated_at_us=_now_us()
            )

        self._cache_put(cache_key, decision)
        self._emit_audit(call, ctx, decision, started_at)
        return self._shadow_override(decision)

    # --- Internals ---

    def _check_restricted(
        self, call: ToolCall, ctx: PolicyContext
    ) -> Decision | None:
        """Return a DENY/ALLOW if restricted mode applies, else None."""
        if self._max_bundle_age is None:
            return None
        if ctx.bundle_age_seconds <= self._max_bundle_age:
            return None
        if call.tool_name in self._safe_set:
            return Decision.allow(
                input_hash=_hash_call(call), evaluated_at_us=_now_us()
            )
        return Decision.deny(
            layer="pipeline",
            rule_id="restricted_mode",
            reason=(
                f"Policy bundle age {ctx.bundle_age_seconds:.0f}s exceeds "
                f"max {self._max_bundle_age:.0f}s; only safe-set tools permitted. "
                f"Tool {call.tool_name!r} not in safe set."
            ),
            input_hash=_hash_call(call),
            evaluated_at_us=_now_us(),
        )

    def _shadow_override(self, decision: Decision) -> Decision:
        """In shadow mode, force-allow every evaluated call."""
        if not self._shadow or decision.outcome == "allow":
            return decision
        return Decision.allow(
            input_hash=decision.input_hash,
            evaluated_at_us=decision.evaluated_at_us,
        )

    def _cache_key(self, call: ToolCall) -> str:
        return (
            f"{call.agent_did}|{call.tool_name}|{call.classification}|{_hash_call(call)}"
        )

    def _cache_get(self, key: str) -> Decision | None:
        if self._cache_ttl <= 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        decision, stored_at = entry
        if (self._monotonic() - stored_at) > self._cache_ttl:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return decision

    def _cache_put(self, key: str, decision: Decision) -> None:
        if self._cache_ttl <= 0:
            return
        self._cache[key] = (decision, self._monotonic())
        while len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)

    def _emit_audit(
        self,
        call: ToolCall,
        ctx: PolicyContext,
        decision: Decision,
        started_at: float,
        *,
        cache_hit: bool = False,
    ) -> None:
        """Emit a structured audit event per R-060."""
        if self._audit_sink is None:
            return
        payload: dict[str, Any] = {
            "tool_name": call.tool_name,
            "agent_did": call.agent_did,
            "session_id": call.session_id,
            "classification": call.classification,
            "tier": ctx.tier,
            "policy_version": ctx.policy_version,
            "decision": decision.outcome,
            "matched_rule": decision.rule_id,
            "layer": decision.layer,
            "reason": decision.reason,
            "input_hash": decision.input_hash,
            "evaluation_time_us": max(
                1, int((self._monotonic() - started_at) * 1_000_000)
            ),
            "cache_hit": cache_hit,
            "shadow": self._shadow,
        }
        try:
            self._audit_sink("policy.evaluate", payload)
        except Exception:
            _logger.exception("Audit sink raised; continuing")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_pipeline(
    *,
    tier: _Tier,
    global_deny_rules: dict[str, str] | None = None,
    agent_allowlists: dict[str, set[str]] | None = None,
    forbidden_compositions: list[frozenset[str]] | None = None,
    cache_ttl_seconds: float = 30.0,
    max_bundle_age_seconds: float | None = None,
    safe_set: set[str] | None = None,
    shadow: bool = False,
    audit_sink: AuditSink | None = None,
) -> PolicyPipeline:
    """Build a tier-specific policy pipeline.

    Tier matrix (SPEC-017 R-010):

    =========== ==================================================
    Tier        Layers
    =========== ==================================================
    federal     global, provider, agent, team, sandbox  (5 layers)
    enterprise  global, provider, agent, sandbox        (4 layers)
    personal    global                                  (1 layer)
    =========== ==================================================

    Args:
        tier: Deployment tier.
        global_deny_rules: Tool name → denial reason mapping.
        agent_allowlists: Agent DID → allowed tool name set.
        forbidden_compositions: Sets of capability tags that are forbidden
            when held by a single batch (non-compositional safety).
        cache_ttl_seconds: Decision cache TTL (0 disables).
        max_bundle_age_seconds: Stale bundle threshold (None disables).
        safe_set: Tools permitted in restricted mode.
        shadow: Enable shadow mode (log denials, always allow).
        audit_sink: Callback for structured audit events.

    Returns:
        Configured PolicyPipeline ready for evaluation.
    """
    g = GlobalLayer(
        deny_rules=global_deny_rules or {},
        forbidden_compositions=forbidden_compositions or [],
    )
    layers: list[PolicyLayer]

    if tier == "personal":
        layers = [g]
    elif tier == "enterprise":
        layers = [
            g,
            ProviderLayer(),
            AgentLayer(allowlist_by_agent=agent_allowlists or {}),
            SandboxLayer(),
        ]
    else:  # federal
        layers = [
            g,
            ProviderLayer(),
            AgentLayer(allowlist_by_agent=agent_allowlists or {}),
            TeamLayer(),
            SandboxLayer(),
        ]

    return PolicyPipeline(
        layers=layers,
        cache_ttl_seconds=cache_ttl_seconds,
        max_bundle_age_seconds=max_bundle_age_seconds,
        safe_set=safe_set,
        shadow=shadow,
        audit_sink=audit_sink,
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _now_us() -> int:
    """Return current monotonic time in microseconds."""
    return int(time.monotonic() * 1_000_000)


def _hash_call(call: ToolCall) -> str:
    """Deterministic hash of the tool call payload for cache keys and audit.

    SHA-256 of stable JSON representation, truncated to 16 hex chars.
    """
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


__all__ = [
    "AgentLayer",
    "AuditSink",
    "Decision",
    "GlobalLayer",
    "PolicyContext",
    "PolicyLayer",
    "PolicyPipeline",
    "ProviderLayer",
    "SandboxLayer",
    "TeamLayer",
    "TierConfig",
    "ToolCall",
    "build_pipeline",
]
