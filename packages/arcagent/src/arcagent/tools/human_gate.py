"""Human approval gate for lethal-trifecta completion — SPEC-035 REQ-014/015/016.

When arctrust's ``GlobalLayer`` denies a call with
``rule_id="global.forbidden_composition"``, the completing action must not
silently proceed nor silently die: it PAUSES for explicit human approval
(ASI09). This module orchestrates that pause. The *decision* ("this composition
is forbidden") is arctrust's; the *orchestration* ("pause and ask a human") is
arcagent's — the clean handoff is a one-shot, operator-signed approval token
(:class:`arctrust.ApprovalGrant`) that arctrust then verifies.

Key invariants:
- **Fail closed.** Denial or timeout → deny the completing call (return None).
- **Agent cannot self-approve.** The token is signed by the *operator* key
  (SPEC-053 authority), never the agent DID. The agent has no path to mint it.
- **Per-action, unless the operator said "Always allow".** A one-shot approval
  admits exactly one call (the grant binds to the call hash). An operator may
  instead store a standing interactive grant (SPEC-035 OQ-3, ruled 2026-10-03):
  it covers later calls by the same agent whose legs sit inside the approved
  composition and whose egress goes to the approved destination. Every use is
  audited; a revoke takes effect on the next call; federal never honours one.
- **Tier stringency (ADR-019).** Federal never auto-approves. Personal/
  enterprise may auto-approve *named* low-risk compositions via explicit config.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from arctrust.policy import (
    ApprovalGrant,
    OperatorApprovalAuthority,
    ScenarioGrant,
    ToolCall,
    sign_approval,
    verify_approval,
    verify_interactive_grant,
)
from arctrust.signer import Signer

_logger = logging.getLogger("arcagent.human_gate")

# An approval channel surfaces the request to a human via a MECHANICAL,
# operator-authenticated surface (the arcstore-backed `arc approve` CLI / arcui
# operator action — never agent chat, which a prompt-injected or foreign message
# could forge) and returns the operator-signed ``ApprovalGrant`` for THIS call,
# or ``None`` if denied/timed-out. The gate VERIFIES the returned grant against
# the operator public key (:func:`verify_approval`) before it counts — the agent
# never mints its own approval for the human path (ASI09). Fail-closed on any
# exception/timeout is enforced by the gate, not the channel.
ApprovalChannel = Callable[["ApprovalRequest"], Awaitable["ApprovalGrant | None"]]
AuditSink = Callable[[str, dict[str, Any]], None]


@runtime_checkable
class StandingGrantSource(Protocol):
    """Where an agent's stored "Always allow" grants are read and counted.

    Read fresh on every gate hit (a revoke must bite on the next call). The
    source holds data only: the gate verifies every signature and pins the
    signer to the deployment operator itself.
    """

    async def active_standing_grants(self, agent_did: str) -> Sequence[tuple[str, ScenarioGrant]]:
        """``(grant id, grant)`` for every active row of this agent."""
        ...

    async def record_standing_grant_use(self, grant_id: str, agent_did: str) -> bool:
        """Count one use; False when the grant is no longer active."""
        ...


# Bounds on the argument preview surfaced to the operator. Each value is capped
# hard (LLM02 — a huge tool argument must not inflate the approval row, the audit
# log, or the operator surface); the one-line provenance summary is capped again.
_MAX_ARG_VALUE_LEN = 120
_MAX_ARG_SUMMARY_LEN = 200


def preview_arguments(arguments: Mapping[str, object]) -> dict[str, str]:
    """Return a length-bounded per-argument preview for operator triage.

    The gate shows the arguments AS IT RECEIVED THEM. Which values an operator
    may see is the deployment's PII policy, owned by ``arcllm`` and already
    applied before a call reaches here; deciding it a second time here made Arc
    stricter with the human approving an action than with the model that
    proposed it. Bounding the size of the row IS this module's concern (LLM02).
    """
    preview: dict[str, str] = {}
    for name, value in arguments.items():
        rendered = str(value)
        if len(rendered) > _MAX_ARG_VALUE_LEN:
            rendered = rendered[:_MAX_ARG_VALUE_LEN] + "..."
        preview[name] = rendered
    return preview


def summarize_arguments(arguments: Mapping[str, object]) -> str:
    """Return a one-line bounded argument summary for a provenance entry."""
    line = ", ".join(f"{name}={value}" for name, value in preview_arguments(arguments).items())
    return line[:_MAX_ARG_SUMMARY_LEN]


@dataclass(frozen=True)
class ApprovalRequest:
    """Agent-originated approval request (labeled as such — ASI09).

    Beyond the tool/legs/hash the gate needs, carries the triage context an
    operator needs to decide a trifecta block (SPEC-035 approval enrichment):
    ``arguments`` (bounded preview of WHAT is being acted on), ``leg_provenance``
    (which prior calls lit each leg, and when), and the ``session_id``.
    """

    tool_name: str
    agent_did: str
    legs: frozenset[str]
    call_hash: str
    arguments: dict[str, str] = field(default_factory=dict)
    leg_provenance: list[dict[str, object]] = field(default_factory=list)
    session_id: str = ""
    origin: str = "agent"  # never impersonate a human
    #: Destination class of the egress this call adds; ``None`` when it adds none.
    destination: str | None = None
    #: The verb a standing grant would be scoped to (unqualified tool name).
    grant_tool: str = ""
    #: Whether the operator may answer "Always allow" (never at federal).
    standing_eligible: bool = False


@dataclass
class HumanGateConfig:
    """Runtime config for the gate (mirrors the [tools.human_gate] TOML block)."""

    timeout_seconds: float = 300.0
    # Personal/enterprise only: leg-sets that may be auto-approved without a
    # human, e.g. [["private_data", "external_comms", "untrusted_input"]].
    auto_approve: list[frozenset[str]] = field(default_factory=list)
    # Personal/enterprise only: specific tool names that skip the gate
    # regardless of which legs tripped it, e.g. {"jira_create_issue"}. Narrower
    # to configure than naming every leg-composition a tool might trip, and
    # scoped the other way from ``auto_approve`` — a tool named here is trusted
    # no matter the composition; a composition named there is trusted no matter
    # the tool. Same federal exclusion applies (ADR-019).
    auto_approve_tools: frozenset[str] = field(default_factory=frozenset)


class HumanGate:
    """Pause a trifecta-completing call for explicit human approval.

    Parameters
    ----------
    operator_signer:
        The deployment's operator :class:`~arctrust.signer.Signer` (SPEC-053
        audit/approval authority — in-process or vault_transit). Mints approval
        tokens — NOT the agent key (ASI09). Under vault_transit the operator seed
        never enters this process; approvals sign by reference (SPEC-037 F1).
    agent_did:
        The subject agent's DID (audit labeling + self-approval guard).
    tier:
        Deployment tier; federal forbids auto-approve (REQ-016).
    config:
        Timeout + named auto-approve compositions.
    audit_sink:
        ``(event, payload)`` callback for grant/deny/timeout records (REQ-014 AC2).
    channel:
        Async callable that surfaces the request to a human and returns the
        decision. ``None`` → no human reachable → fail closed (deny).
    """

    def __init__(
        self,
        *,
        operator_signer: Signer,
        agent_did: str,
        tier: str,
        config: HumanGateConfig | None = None,
        audit_sink: AuditSink | None = None,
        channel: ApprovalChannel | None = None,
        standing_grants: StandingGrantSource | None = None,
    ) -> None:
        self._agent_did = agent_did
        self._tier = tier
        self._config = config or HumanGateConfig()
        self._audit_sink = audit_sink
        self._channel = channel
        self._standing = standing_grants
        self._operator = OperatorApprovalAuthority(operator_signer)

    async def request(
        self,
        call: ToolCall,
        *,
        legs: frozenset[str],
        provenance: list[dict[str, object]] | None = None,
        destination: str | None = None,
        grant_tool: str | None = None,
        may_stand: bool = False,
    ) -> ApprovalGrant | None:
        """Obtain a one-shot approval for ``call`` or return None (fail closed).

        ``legs`` is the accumulated forbidden union that tripped the gate — used
        for auto-approve matching and for labeling the request. ``provenance`` is
        the ordered list of prior calls that lit each leg, threaded through so the
        operator can triage the composition. ``destination`` is the destination
        class of the egress this call adds (``None``: it adds none) and
        ``grant_tool`` the verb a standing grant is scoped to (default: the
        call's tool) — together they decide whether an operator's standing
        "Always allow" already covers this call. ``may_stand`` is opt-in: only
        the dispatch-time composition gates (the registry's trifecta gate and a
        connection's outbound gate) pass it, so a standing grant never answers a
        tier-policy approval, a workflow activation, or any other kind of ask.
        """
        from arctrust.policy import _hash_call

        request = ApprovalRequest(
            tool_name=call.tool_name,
            agent_did=call.agent_did,
            legs=legs,
            call_hash=_hash_call(call),
            arguments=preview_arguments(call.arguments),
            leg_provenance=provenance or [],
            session_id=call.session_id,
            destination=destination,
            grant_tool=grant_tool or call.tool_name,
            standing_eligible=may_stand and bool(legs) and self._tier != "federal",
        )

        if self._auto_approvable(legs, call.tool_name):
            self._emit("human_gate.auto_approved", request, outcome="auto_approve")
            return sign_approval(call, self._operator)

        grant_id = await self._standing_grant_covering(request)
        if grant_id is not None:
            self._emit(
                "human_gate.standing_grant_used", request, outcome="granted", grant_id=grant_id
            )
            return sign_approval(call, self._operator)

        if self._channel is None:
            self._emit("human_gate.denied", request, outcome="no_channel")
            return None

        grant = await self._ask_human(request)
        if grant is None:
            self._emit("human_gate.denied", request, outcome="denied_or_timeout")
            return None

        # The grant came from an out-of-process operator surface — trust nothing
        # until it both verifies (bound to THIS call_hash, valid signature, not the
        # agent's own DID — ASI09) AND is pinned to the DEPLOYMENT operator. Pinning
        # is what stops a foreign actor: verify_approval alone accepts any non-agent
        # key, so without the DID pin any keypair could self-mint an approval.
        if not verify_approval(call, grant) or grant.approver_did != self._operator.did:
            self._emit("human_gate.denied", request, outcome="invalid_grant")
            return None

        self._emit("human_gate.granted", request, outcome="granted")
        return grant

    def _auto_approvable(self, legs: frozenset[str], tool_name: str) -> bool:
        """Personal/enterprise may auto-approve named compositions or tools; federal never.

        Two independent ways in, either is sufficient:
        - ``auto_approve``: the leg-set must equal the tripping composition
          EXACTLY. A subset test would let a narrower entry (e.g.
          ``{private_data, external_comms}``) green-light a wider forbidden set
          — the tripping union is always a superset of any subset — silently
          authorizing more than the operator named.
        - ``auto_approve_tools``: the tool name is trusted outright, whatever
          composition tripped it this time. Use for a specific tool the
          operator has decided never needs a human in the loop (e.g. a
          low-stakes connector write), rather than enumerating every leg-set
          that tool might ever trip.
        """
        if self._tier == "federal":
            return False
        if tool_name in self._config.auto_approve_tools:
            return True
        return any(named == legs for named in self._config.auto_approve)

    async def _standing_grant_covering(self, request: ApprovalRequest) -> str | None:
        """The id of an operator standing grant that covers ``request``, or None.

        Fail closed throughout: never at federal, any read error means "not
        covered" (the human is asked), every candidate is verified here and
        pinned to the deployment operator, and the use only counts if the row
        is still active at the moment it is recorded — a revoke racing this
        call wins.
        """
        source = self._standing
        if source is None or not request.standing_eligible:
            return None
        try:
            candidates = await source.active_standing_grants(request.agent_did)
        except Exception:  # reason: fail-closed — an unreadable store asks the human
            _logger.exception("Standing grants unreadable; asking the operator")
            return None
        for grant_id, grant in candidates:
            if grant.approver_did != self._operator.did:
                continue
            if not verify_interactive_grant(
                grant,
                agent_did=request.agent_did,
                tool_name=request.grant_tool,
                legs=request.legs,
                destination=request.destination,
                tier=self._tier,
            ):
                continue
            try:
                if await source.record_standing_grant_use(grant_id, request.agent_did):
                    return grant_id
            except Exception:  # reason: fail-closed — an unrecorded use is no use
                _logger.exception("Standing grant use could not be recorded; asking")
                return None
        return None

    async def _ask_human(self, request: ApprovalRequest) -> ApprovalGrant | None:
        """Surface the request to the operator channel; fail closed on timeout/error.

        Returns the operator-signed grant (unverified here — the caller verifies)
        or None on denial/timeout/error. The gate owns the timeout so a channel
        that blocks forever still fails closed.
        """
        channel = self._channel
        if channel is None:
            return None
        try:
            return await asyncio.wait_for(channel(request), timeout=self._config.timeout_seconds)
        except TimeoutError:
            return None
        except Exception:  # reason: fail-closed — any channel error denies
            _logger.exception("Approval channel raised; failing closed")
            return None

    def _emit(
        self, event: str, request: ApprovalRequest, *, outcome: str, grant_id: str = ""
    ) -> None:
        if self._audit_sink is None:
            return
        try:
            self._audit_sink(
                event,
                {
                    "grant_id": grant_id,
                    "destination": request.destination,
                    "tool": request.tool_name,
                    "agent_did": request.agent_did,
                    "operator_did": self._operator.did,
                    "legs": sorted(request.legs),
                    "call_hash": request.call_hash,
                    "arguments": request.arguments,
                    "leg_provenance": request.leg_provenance,
                    "session_id": request.session_id,
                    "outcome": outcome,
                    "origin": request.origin,
                    "tier": self._tier,
                },
            )
        except Exception:  # reason: fail-open — audit must not mask the decision
            _logger.exception("Human-gate audit sink raised; continuing")


__all__ = [
    "ApprovalChannel",
    "ApprovalRequest",
    "HumanGate",
    "HumanGateConfig",
    "StandingGrantSource",
    "preview_arguments",
    "summarize_arguments",
]
