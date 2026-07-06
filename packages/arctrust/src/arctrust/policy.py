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

from arctrust.identity import AgentIdentity, did_matches_pubkey
from arctrust.keypair import verify as _ed25519_verify

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

    Authentication fields (``public_key`` + ``signature``) bind the call to a
    specific keypair. A call is *authenticated* only when ``signature`` is a
    valid Ed25519 signature over :meth:`signing_bytes` under ``public_key``,
    AND ``public_key``'s fingerprint matches ``agent_did`` (see
    :func:`arctrust.identity.did_matches_pubkey`). Both are ``None`` on an
    unsigned call, which the IdentityLayer denies fail-closed.
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str
    arguments: dict[str, Any]
    agent_did: str
    session_id: str
    classification: str
    parent_call_id: str | None = None
    public_key: bytes | None = None
    signature: bytes | None = None

    def signing_bytes(self) -> bytes:
        """Canonical bytes the signature covers — every field except the auth pair.

        Deterministic JSON (sorted keys) over the authenticated content. The
        ``public_key``/``signature`` fields are excluded: they ARE the
        attestation, they cannot also be inside what is attested.
        """
        payload = json.dumps(
            {
                "tool_name": self.tool_name,
                "arguments": self.arguments,
                "agent_did": self.agent_did,
                "session_id": self.session_id,
                "classification": self.classification,
                "parent_call_id": self.parent_call_id,
            },
            sort_keys=True,
            default=str,
        )
        return payload.encode("utf-8")


class ProviderUsage(BaseModel):
    """LLM-provider consumption for the current call — filled by SPEC-038.

    SPEC-034 (this spec) defines the schema only; the accounting service
    (SPEC-038) measures usage against the arcrun/arcllm call surface and
    populates this at dispatch. ``ProviderLayer`` reads it as injected state
    and never computes it.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    tokens_used: int
    cost_used: float
    requests_in_window: int


class TeamScope(BaseModel):
    """Team role + delegation grant for the calling agent — filled by arcteam.

    ``authorized_tools`` is the role's activated capability scope for this
    call; ``delegation_grant``, when present, is the (never-wider) scope a
    delegated (``parent_call_id``-carrying) call may reach. SPEC-034 reads and
    gates; arcteam derives membership and role meaning.
    """

    model_config = ConfigDict(frozen=True)

    role: str
    authorized_tools: frozenset[str]
    delegation_grant: frozenset[str] | None = None


class ToolRuntimeStatus(BaseModel):
    """Per-tool verification + isolation status — filled by SPEC-033/036.

    ``verified`` is the load-time answer from SPEC-033 (sign/verify + TOFU);
    ``required_isolation``/``available_isolation`` are compared over the
    SPEC-036 isolation ladder (``host`` < ``container`` < ``vm``). SPEC-034
    reads the answers and gates; it re-runs no verification and starts no
    sandbox.
    """

    model_config = ConfigDict(frozen=True)

    verified: bool
    required_isolation: str
    available_isolation: str


class PolicyContext(BaseModel):
    """Runtime context for policy evaluation.

    Base fields (``tier``, ``policy_version``, ``bundle_age_seconds``) are the
    original contract. The optional state fields carry the injected inputs the
    real ProviderLayer/TeamLayer/SandboxLayer compare against; each defaults to
    ``None`` so existing 3-field constructions stay valid (REQ-014) and is
    populated by its owning spec (REQ-015).
    """

    model_config = ConfigDict(frozen=True)

    tier: _Tier
    policy_version: str
    bundle_age_seconds: float
    provider_usage: ProviderUsage | None = None
    team_scope: TeamScope | None = None
    tool_runtime: ToolRuntimeStatus | None = None


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
# Call attestation — sign / verify a ToolCall
# ---------------------------------------------------------------------------


def sign_call(call: ToolCall, identity: AgentIdentity) -> ToolCall:
    """Return a copy of ``call`` signed by ``identity`` (pubkey + signature set).

    The call's ``agent_did`` is overwritten with the signer's DID so a caller
    cannot sign content while claiming a different identity. Raises if the
    identity cannot sign (verify-only).
    """
    bound = call.model_copy(update={"agent_did": identity.did})
    signature = identity.sign(bound.signing_bytes())
    return bound.model_copy(update={"public_key": identity.public_key, "signature": signature})


def verify_call(call: ToolCall) -> bool:
    """Whether ``call`` is authentically signed by the holder of ``agent_did``.

    Three conditions, all required (fail-closed — any failure returns False):
      1. The call carries both a ``public_key`` and a ``signature``.
      2. ``public_key``'s fingerprint matches ``agent_did`` — the presented key
         is the one the DID was minted from (defeats DID impersonation).
      3. ``signature`` is a valid Ed25519 signature over ``signing_bytes()``
         under ``public_key`` — proves possession of the private key and that
         the call content was not tampered with after signing.
    """
    if call.public_key is None or call.signature is None:
        return False
    if not did_matches_pubkey(call.agent_did, call.public_key):
        return False
    return _ed25519_verify(call.signing_bytes(), call.signature, call.public_key)


# ---------------------------------------------------------------------------
# Concrete layer implementations
# ---------------------------------------------------------------------------


class IdentityLayer:
    """Authentication gate — the FIRST layer, fail-closed, at every tier.

    Enforces the SSH-key invariant: a call runs only if it is signed by an
    agent that holds the private key for the DID it claims. When
    ``require_registered`` is set (enterprise/federal), the agent's DID must
    also appear in ``registry`` AND the registered pubkey must match the call's
    key — deny-by-default admission. Personal tier admits any validly
    self-signed agent (``require_registered=False``).

    ``registry`` maps ``agent_did -> ed25519 public key (32 bytes)``.
    """

    name = "identity"

    def __init__(self, *, registry: dict[str, bytes], require_registered: bool) -> None:
        self._registry = registry
        self._require_registered = require_registered

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        now_us = _now_us()
        input_hash = _hash_call(call)

        if not verify_call(call):
            return Decision.deny(
                layer=self.name,
                rule_id="identity.unsigned_or_invalid",
                reason=(
                    f"Call for {call.tool_name!r} is not validly signed by "
                    f"{call.agent_did!r}: missing/forged signature or the public "
                    "key does not match the claimed DID."
                ),
                input_hash=input_hash,
                evaluated_at_us=now_us,
            )

        if self._require_registered:
            registered = self._registry.get(call.agent_did)
            if registered is None or bytes(registered) != bytes(call.public_key or b""):
                return Decision.deny(
                    layer=self.name,
                    rule_id="identity.not_admitted",
                    reason=(
                        f"Agent {call.agent_did!r} is not in the admitted-agent "
                        "registry (or its registered key does not match). "
                        "Enterprise/federal deny unknown agents by default."
                    ),
                    input_hash=input_hash,
                    evaluated_at_us=now_us,
                )

        return Decision.allow(input_hash=input_hash, evaluated_at_us=now_us)


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


class ProviderLimit(BaseModel):
    """Per-provider budget + rate ceiling — a deployment-policy floor (LLM10)."""

    model_config = ConfigDict(frozen=True)

    max_tokens: int
    max_cost: float
    max_requests: int


class ProviderLayer:
    """LLM provider budget and rate-limit gate — a pure comparator (LLM10).

    Limits come from construction (deployment policy); current usage comes
    from ``PolicyContext.provider_usage`` (filled by SPEC-038). The layer never
    calls arcllm, never decrements, and holds no mutable usage store.

    Configured-gate semantics: with no limits configured the layer is a no-op
    and always allows — absence of a budget policy is not a violation. Once a
    limit IS configured, missing usage telemetry fails closed
    (``provider.state_missing``): a real budget with a blind meter cannot be
    proven within bounds (REQ-004, REQ-005).
    """

    name = "provider"

    def __init__(
        self,
        *,
        limits_by_provider: dict[str, ProviderLimit] | None = None,
    ) -> None:
        self._limits = limits_by_provider or {}

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        now_us = _now_us()
        input_hash = _hash_call(call)

        if not self._limits:
            return Decision.allow(input_hash=input_hash, evaluated_at_us=now_us)

        usage = ctx.provider_usage
        if usage is None:
            return Decision.deny(
                layer=self.name,
                rule_id="provider.state_missing",
                reason=(
                    "Provider budget policy is configured but no usage state is in "
                    "context; fail closed on the telemetry gap (SPEC-038 populates it)."
                ),
                input_hash=input_hash,
                evaluated_at_us=now_us,
            )

        limit = self._limits.get(usage.provider)
        if limit is None:
            return Decision.allow(input_hash=input_hash, evaluated_at_us=now_us)

        if usage.tokens_used >= limit.max_tokens or usage.cost_used >= limit.max_cost:
            return Decision.deny(
                layer=self.name,
                rule_id="provider.budget_exceeded",
                reason=(
                    f"Provider {usage.provider!r} budget exceeded: "
                    f"tokens {usage.tokens_used}/{limit.max_tokens}, "
                    f"cost {usage.cost_used}/{limit.max_cost}."
                ),
                input_hash=input_hash,
                evaluated_at_us=now_us,
            )

        if usage.requests_in_window >= limit.max_requests:
            return Decision.deny(
                layer=self.name,
                rule_id="provider.rate_exceeded",
                reason=(
                    f"Provider {usage.provider!r} rate exceeded: "
                    f"requests {usage.requests_in_window}/{limit.max_requests}."
                ),
                input_hash=input_hash,
                evaluated_at_us=now_us,
            )

        return Decision.allow(input_hash=input_hash, evaluated_at_us=now_us)


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
    """Team-scoped delegation gate — capability-scoping comparator (ASI03/ASI07).

    Construction supplies the static role->scope floor (``roles``); the
    per-call activated scope and any delegation grant arrive on
    ``PolicyContext.team_scope`` (filled by arcteam/arcagent). Absence of a
    team scope is not a violation — admission of unknown agents is the
    IdentityLayer's job (REQ-009).
    """

    name = "team"

    def __init__(self, *, roles: dict[str, frozenset[str]] | None = None) -> None:
        self._roles = roles or {}

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        now_us = _now_us()
        input_hash = _hash_call(call)

        scope = ctx.team_scope
        if scope is None:
            return Decision.allow(input_hash=input_hash, evaluated_at_us=now_us)

        # Static role floor if configured, else the context's activated scope.
        authorized = self._roles.get(scope.role, scope.authorized_tools)
        if call.tool_name not in authorized:
            return Decision.deny(
                layer=self.name,
                rule_id="team.scope_violation",
                reason=(
                    f"Tool {call.tool_name!r} is outside role {scope.role!r} scope "
                    f"{sorted(authorized)}."
                ),
                input_hash=input_hash,
                evaluated_at_us=now_us,
            )

        # A delegated call (carries a parent) may not exceed its grant's scope.
        if (
            call.parent_call_id is not None
            and scope.delegation_grant is not None
            and call.tool_name not in scope.delegation_grant
        ):
            return Decision.deny(
                layer=self.name,
                rule_id="team.delegation_exceeded",
                reason=(
                    f"Delegated call for {call.tool_name!r} exceeds its grant "
                    f"{sorted(scope.delegation_grant)}."
                ),
                input_hash=input_hash,
                evaluated_at_us=now_us,
            )

        return Decision.allow(input_hash=input_hash, evaluated_at_us=now_us)


# Ordered isolation ladder owned by SPEC-036's vocabulary. SPEC-034 references
# the ordering to compare; it defines no isolation mechanics.
_ISOLATION_LADDER = ("host", "container", "vm")


def _isolation_satisfies(available: str, required: str) -> bool:
    """True iff ``available`` isolation is at least as strong as ``required``.

    Unknown ``required`` fails closed (unsatisfiable); unknown ``available``
    ranks below the ladder floor so it satisfies nothing.
    """
    if required not in _ISOLATION_LADDER:
        return False
    required_rank = _ISOLATION_LADDER.index(required)
    available_rank = (
        _ISOLATION_LADDER.index(available) if available in _ISOLATION_LADDER else -1
    )
    return available_rank >= required_rank


class SandboxLayer:
    """Dynamic-tool / isolation policy gate — deliberately thin (ASI04/ASI05).

    Reads verification status (SPEC-033) and isolation availability (SPEC-036)
    from ``PolicyContext.tool_runtime`` and compares. It re-runs no signature
    verification and starts no sandbox. With no runtime status in context the
    layer is a no-op and allows: the SPEC-033 load gate already verified any
    tool that reached the registry, so there is nothing for this layer to add
    when blind. It gates only when a status IS present (REQ-010..013).
    """

    name = "sandbox"

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        now_us = _now_us()
        input_hash = _hash_call(call)

        rt = ctx.tool_runtime
        if rt is None:
            return Decision.allow(input_hash=input_hash, evaluated_at_us=now_us)

        if not rt.verified:
            return Decision.deny(
                layer=self.name,
                rule_id="sandbox.unverified_tool",
                reason=f"Tool {call.tool_name!r} is unverified/dynamic (SPEC-033).",
                input_hash=input_hash,
                evaluated_at_us=now_us,
            )

        if not _isolation_satisfies(rt.available_isolation, rt.required_isolation):
            return Decision.deny(
                layer=self.name,
                rule_id="sandbox.isolation_unsatisfiable",
                reason=(
                    f"Required isolation {rt.required_isolation!r} exceeds available "
                    f"{rt.available_isolation!r} (SPEC-036 ladder)."
                ),
                input_hash=input_hash,
                evaluated_at_us=now_us,
            )

        return Decision.allow(input_hash=input_hash, evaluated_at_us=now_us)


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
            raise ValueError(f"Unknown tier {tier!r}. Must be one of: {list(configs.keys())}")
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
            except Exception as exc:  # reason: fail-open — log + continue
                _logger.exception("Policy layer %r raised — failing closed (R-012)", layer.name)
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
            decision = Decision.allow(input_hash=_hash_call(call), evaluated_at_us=_now_us())

        self._cache_put(cache_key, decision)
        self._emit_audit(call, ctx, decision, started_at)
        return self._shadow_override(decision)

    # --- Internals ---

    def _check_restricted(self, call: ToolCall, ctx: PolicyContext) -> Decision | None:
        """Return a DENY/ALLOW if restricted mode applies, else None."""
        if self._max_bundle_age is None:
            return None
        if ctx.bundle_age_seconds <= self._max_bundle_age:
            return None
        if call.tool_name in self._safe_set:
            return Decision.allow(input_hash=_hash_call(call), evaluated_at_us=_now_us())
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
        return f"{call.agent_did}|{call.tool_name}|{call.classification}|{_hash_call(call)}"

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
            "rule_id": decision.rule_id,
            "layer": decision.layer,
            "reason": decision.reason,
            "input_hash": decision.input_hash,
            "evaluation_time_us": max(1, int((self._monotonic() - started_at) * 1_000_000)),
            "cache_hit": cache_hit,
            "shadow": self._shadow,
        }
        try:
            self._audit_sink("policy.evaluate", payload)
        except Exception:  # reason: fail-open — log + continue
            _logger.exception("Audit sink raised; continuing")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_pipeline(
    *,
    tier: _Tier,
    agent_registry: dict[str, bytes] | None = None,
    global_deny_rules: dict[str, str] | None = None,
    agent_allowlists: dict[str, set[str]] | None = None,
    provider_limits: dict[str, ProviderLimit] | None = None,
    team_roles: dict[str, frozenset[str]] | None = None,
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
        provider_limits: Provider name → ProviderLimit floor (ProviderLayer).
        team_roles: Role → authorized tool scope floor (TeamLayer).
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
    # IdentityLayer runs first at EVERY tier — authentication is universal
    # (ADR-019). enterprise/federal additionally deny agents that are not in
    # the admitted-agent registry; personal admits any validly self-signed key.
    identity = IdentityLayer(
        registry=agent_registry or {},
        require_registered=tier in ("enterprise", "federal"),
    )
    g = GlobalLayer(
        deny_rules=global_deny_rules or {},
        forbidden_compositions=forbidden_compositions or [],
    )
    # Provider/Sandbox are built only for enterprise/federal. Each is a no-op
    # when its policy is unconfigured (empty limits / no runtime state) and only
    # fails closed once a configured policy meets missing telemetry (SPEC-034).
    provider = ProviderLayer(limits_by_provider=provider_limits or {})
    sandbox = SandboxLayer()
    layers: list[PolicyLayer]

    if tier == "personal":
        layers = [identity, g]
    elif tier == "enterprise":
        layers = [
            identity,
            g,
            provider,
            AgentLayer(allowlist_by_agent=agent_allowlists or {}),
            sandbox,
        ]
    else:  # federal
        layers = [
            identity,
            g,
            provider,
            AgentLayer(allowlist_by_agent=agent_allowlists or {}),
            TeamLayer(roles=team_roles or {}),
            sandbox,
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
    "IdentityLayer",
    "PolicyContext",
    "PolicyLayer",
    "PolicyPipeline",
    "ProviderLayer",
    "ProviderLimit",
    "ProviderUsage",
    "SandboxLayer",
    "TeamLayer",
    "TeamScope",
    "TierConfig",
    "ToolCall",
    "ToolRuntimeStatus",
    "build_pipeline",
    "sign_call",
    "verify_call",
]
