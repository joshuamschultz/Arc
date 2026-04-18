"""Tool Policy Pipeline — SPEC-017 §3.1.

Five-layer, first-DENY-wins, fail-closed evaluation pipeline. Every tool
call flows through this pipeline; there is no sudo path.

Ordering (tier-dependent): Global → Provider → Agent → Team → Sandbox.
  * Federal: all 5 layers
  * Enterprise: 4 (no Team)
  * Personal: 1 (Global only)

Pillars — Simplicity first, Security dominant:
  * One class per responsibility (pipeline, decision, layer)
  * No hidden state between calls (cache is explicit, bounded)
  * Exceptions → DENY (never propagate as ALLOW)
  * Structured deny reasons: answer which-layer, which-rule, what-inputs

This module is pure logic. It emits audit events through an injected
sink and has no knowledge of OTel, the module bus, or the tool
registry. Those integrations live in ``tool_registry.py``.
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

from arcagent.core.errors import ArcAgentError

_logger = logging.getLogger("arcagent.tool_policy")

AuditSink = Callable[[str, dict[str, Any]], None]
MonotonicClock = Callable[[], float]


# --- Data models -----------------------------------------------------------


class ToolCall(BaseModel):
    """Immutable request to invoke a tool.

    Carries agent identity, classification context, and call-chain
    lineage (``parent_call_id``) so classification propagation can be
    verified across delegations.
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


class PolicyDenied(ArcAgentError):  # noqa: N818 — domain convention; peers are *Error/ArcAgentError
    """Raised by tool dispatch when the policy pipeline returns DENY.

    Carries the full :class:`Decision` so callers and auditors can see
    which layer denied, which rule matched, and why.
    """

    _component = "tool_policy"

    def __init__(self, decision: Decision) -> None:
        # Format: "[layer:rule_id] reason" so the three questions are
        # answered even when only str(err) is logged.
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


# --- Layer protocol + concrete implementations ----------------------------


@runtime_checkable
class PolicyLayer(Protocol):
    """Single decision boundary within the pipeline.

    Each layer must be stateless w.r.t. a single evaluation — internal
    caches and indexes are acceptable but must not mutate across calls.
    Layers MUST NOT raise; any exception is caught and converted to
    ``Decision.deny(..., rule_id="layer_error")`` by the pipeline.
    """

    name: str

    async def evaluate(
        self, call: ToolCall, ctx: PolicyContext
    ) -> Decision: ...


class GlobalLayer:
    """Tenant-wide rules and forbidden capability compositions.

    Denylist is an indexed dict[tool_name -> reason] for O(1) lookup.
    Forbidden compositions are NOT evaluated here (handled per-batch by
    the runtime); this class only denies single tools.
    """

    name = "global"

    def __init__(
        self,
        *,
        deny_rules: dict[str, str],
        forbidden_compositions: list[frozenset[str]],
    ) -> None:
        self._deny_rules = deny_rules
        self._forbidden_compositions = forbidden_compositions

    async def evaluate(
        self, call: ToolCall, ctx: PolicyContext
    ) -> Decision:
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
    """LLM provider budget, rate-limits, circuit-breaker gates.

    Stubbed for Phase 2 — concrete wiring happens in Phase 3 when the
    tool registry hooks in. Default behavior is ALLOW so tests remain
    meaningful without a full provider stack.
    """

    name = "provider"

    async def evaluate(
        self, call: ToolCall, ctx: PolicyContext
    ) -> Decision:
        return Decision.allow(
            input_hash=_hash_call(call), evaluated_at_us=_monotonic_us()
        )


class AgentLayer:
    """Per-agent allowlist enforcement.

    An agent appears in ``allowlist_by_agent`` → only listed tools
    permitted. Agents NOT in the map are unconstrained (the global
    layer already handles tenant-wide denies).
    """

    name = "agent"

    def __init__(
        self, *, allowlist_by_agent: dict[str, set[str]]
    ) -> None:
        self._allowlist = allowlist_by_agent

    async def evaluate(
        self, call: ToolCall, ctx: PolicyContext
    ) -> Decision:
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
    """Team-scoped delegation rules. Stubbed — Phase 6 wiring."""

    name = "team"

    async def evaluate(
        self, call: ToolCall, ctx: PolicyContext
    ) -> Decision:
        return Decision.allow(
            input_hash=_hash_call(call), evaluated_at_us=_monotonic_us()
        )


class SandboxLayer:
    """Dynamic-tool runtime constraints. Stubbed — Phase 7 wiring."""

    name = "sandbox"

    async def evaluate(
        self, call: ToolCall, ctx: PolicyContext
    ) -> Decision:
        return Decision.allow(
            input_hash=_hash_call(call), evaluated_at_us=_monotonic_us()
        )


# --- Pipeline --------------------------------------------------------------


class ToolPolicyPipeline:
    """Ordered, short-circuiting, fail-closed policy evaluator.

    Parameters
    ----------
    layers:
        Ordered list of layers. First-DENY-wins.
    cache_ttl_seconds:
        How long a decision is cached keyed on
        ``(agent_did, tool_name, classification, input_hash)``. 0 or
        negative disables caching.
    max_bundle_age_seconds:
        If the policy bundle is older than this, the pipeline enters
        restricted mode and only tools in ``safe_set`` are permitted.
        ``None`` disables this check.
    safe_set:
        Tool names allowed during restricted mode (stale bundle + no
        connection). Ignored when ``max_bundle_age_seconds`` is ``None``.
    shadow:
        When ``True`` the pipeline evaluates and audit-logs normally
        but always returns ``ALLOW`` — used for safe policy rollout.
    audit_sink:
        Callback invoked once per evaluation with ``(event_type,
        payload)``. Never raises; any exception is logged and swallowed.
    monotonic:
        Injection seam for ``time.monotonic`` — tests pass a fake.
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
        # OrderedDict preserves insertion order so oldest entries are
        # evicted first. Small bounded size — memory protection.
        self._cache: OrderedDict[str, tuple[Decision, float]] = OrderedDict()
        self._cache_max = 10_000

    @property
    def layers(self) -> list[PolicyLayer]:
        """Expose layers for introspection (tier factory asserts)."""
        return list(self._layers)

    async def evaluate(
        self, call: ToolCall, ctx: PolicyContext
    ) -> Decision:
        """Run the layered evaluation. First DENY wins. Exceptions are DENY."""
        started_at = self._monotonic()

        # Restricted mode pre-check (stale bundle + offline)
        restricted = self._check_restricted(call, ctx)
        if restricted is not None:
            self._emit_audit(call, ctx, restricted, started_at)
            return self._shadow_override(restricted)

        # Cache check
        cache_key = self._cache_key(call)
        cached = self._cache_get(cache_key)
        if cached is not None:
            self._emit_audit(call, ctx, cached, started_at, cache_hit=True)
            return self._shadow_override(cached)

        # Layered evaluation — first DENY wins, exception → DENY
        decision: Decision | None = None
        for layer in self._layers:
            try:
                decision = await layer.evaluate(call, ctx)
            except Exception as exc:
                _logger.exception(
                    "Policy layer %r raised — failing closed", layer.name
                )
                decision = Decision.deny(
                    layer=layer.name,
                    rule_id="layer_error",
                    reason=f"{type(exc).__name__}: {exc}",
                    input_hash=_hash_call(call),
                    evaluated_at_us=_monotonic_us(),
                )
                break
            if decision.is_deny():
                break

        if decision is None:
            decision = Decision.allow(
                input_hash=_hash_call(call), evaluated_at_us=_monotonic_us()
            )

        self._cache_put(cache_key, decision)
        self._emit_audit(call, ctx, decision, started_at)
        return self._shadow_override(decision)

    # --- Internals --------------------------------------------------------

    def _check_restricted(
        self, call: ToolCall, ctx: PolicyContext
    ) -> Decision | None:
        """Return a DENY/ALLOW if restricted mode applies, else ``None``.

        Restricted mode engages when the policy bundle is too stale to
        trust (control plane unreachable past ``max_bundle_age``).
        Only ``safe_set`` tools are permitted.
        """
        if self._max_bundle_age is None:
            return None
        if ctx.bundle_age_seconds <= self._max_bundle_age:
            return None
        if call.tool_name in self._safe_set:
            return Decision.allow(
                input_hash=_hash_call(call), evaluated_at_us=_monotonic_us()
            )
        return Decision.deny(
            layer="pipeline",
            rule_id="restricted_mode",
            reason=(
                f"Policy bundle age {ctx.bundle_age_seconds:.0f}s exceeds "
                f"max {self._max_bundle_age:.0f}s; only safe-set tools "
                f"permitted. Tool {call.tool_name!r} not in safe set."
            ),
            input_hash=_hash_call(call),
            evaluated_at_us=_monotonic_us(),
        )

    def _shadow_override(self, decision: Decision) -> Decision:
        """In shadow mode, force-allow every evaluated call."""
        if not self._shadow:
            return decision
        if decision.outcome == "allow":
            return decision
        return Decision.allow(
            input_hash=decision.input_hash,
            evaluated_at_us=decision.evaluated_at_us,
        )

    def _cache_key(self, call: ToolCall) -> str:
        return f"{call.agent_did}|{call.tool_name}|{call.classification}|{_hash_call(call)}"

    def _cache_get(self, key: str) -> Decision | None:
        if self._cache_ttl <= 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        decision, stored_at = entry
        if (self._monotonic() - stored_at) > self._cache_ttl:
            # Expired — drop and re-evaluate
            self._cache.pop(key, None)
            return None
        # Refresh LRU ordering on hit
        self._cache.move_to_end(key)
        return decision

    def _cache_put(self, key: str, decision: Decision) -> None:
        if self._cache_ttl <= 0:
            return
        self._cache[key] = (decision, self._monotonic())
        # Bound the cache — drop oldest
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
        """Emit a single structured audit event for this evaluation.

        Every evaluation emits exactly one event, whether allow, deny,
        cache-hit, or restricted. Shadow overrides are recorded with
        the **pre-shadow** decision so audit trails remain truthful.
        """
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
            # Audit failure must never break a tool call — just log.
            _logger.exception("Audit sink raised; continuing")


# --- Factory ---------------------------------------------------------------


def build_pipeline(
    *,
    tier: Literal["federal", "enterprise", "personal"],
    global_deny_rules: dict[str, str] | None = None,
    agent_allowlists: dict[str, set[str]] | None = None,
    forbidden_compositions: list[frozenset[str]] | None = None,
    cache_ttl_seconds: float = 30.0,
    max_bundle_age_seconds: float | None = None,
    safe_set: set[str] | None = None,
    shadow: bool = False,
    audit_sink: AuditSink | None = None,
) -> ToolPolicyPipeline:
    """Build a tier-specific policy pipeline.

    The tier determines which layers are active. Tier matrix (per
    SPEC-017 §5.1 R-010):

    =========== ==================================================
    Tier        Layers
    =========== ==================================================
    federal     global, provider, agent, team, sandbox
    enterprise  global, provider, agent, sandbox   (no team)
    personal    global                             (single layer)
    =========== ==================================================
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

    return ToolPolicyPipeline(
        layers=layers,
        cache_ttl_seconds=cache_ttl_seconds,
        max_bundle_age_seconds=max_bundle_age_seconds,
        safe_set=safe_set,
        shadow=shadow,
        audit_sink=audit_sink,
    )


# --- Non-compositional safety ---------------------------------------------


class ForbiddenCompositionChecker:
    """Reject tool batches whose combined capabilities are forbidden.

    Two individually-safe tools can compose into a forbidden outcome
    (arXiv:2603.15973). Example: ``file_read + network_egress =
    exfiltration``. Each tool declares ``capability_tags``; at batch
    dispatch time we union those tags and check whether any forbidden
    set is a subset of the union.

    Forbidden sets are declared at deployment time, not inferred at
    runtime — the list is audit-visible.
    """

    def __init__(self, *, forbidden: list[frozenset[str]]) -> None:
        self._forbidden = list(forbidden)

    def is_forbidden(self, capabilities: set[str]) -> bool:
        return self.first_forbidden(capabilities) is not None

    def first_forbidden(self, capabilities: set[str]) -> frozenset[str] | None:
        """Return the first matching forbidden set, else ``None``.

        Useful for audit — the returned set identifies *why* the batch
        was rejected without losing info.
        """
        for combo in self._forbidden:
            if combo.issubset(capabilities):
                return combo
        return None


# --- Utilities -------------------------------------------------------------


def _monotonic_us() -> int:
    """Return the current monotonic time in microseconds.

    Policy evaluation is latency-sensitive; we record micros, not millis.
    """
    return int(time.monotonic() * 1_000_000)


def _hash_call(call: ToolCall) -> str:
    """Deterministic hash of the request payload for cache keys + audit.

    Uses ``json.dumps`` with sort_keys for stable ordering, then SHA-256
    truncated to 16 hex chars (sufficient for in-process cache keys).
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
