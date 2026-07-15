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
- **Per-action.** One approval admits exactly one call (the grant binds to the
  call hash). A distinct later call re-triggers the gate.
- **Tier stringency (ADR-019).** Federal never auto-approves. Personal/
  enterprise may auto-approve *named* low-risk compositions via explicit config.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from arctrust.policy import (
    ApprovalGrant,
    OperatorApprovalAuthority,
    ToolCall,
    sign_approval,
    verify_approval,
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

# Bounds on the argument preview surfaced to the operator. Each value is capped
# hard (LLM02 — a huge tool argument must not inflate the approval row, the audit
# log, or the operator surface); the one-line provenance summary is capped again.
_MAX_ARG_VALUE_LEN = 120
_MAX_ARG_SUMMARY_LEN = 200


def _redact(text: str) -> str:
    """Redact PII/secrets from ``text`` via arcllm's regex detector.

    Imported lazily (mirrors ``arcagent.modules.web.capabilities``) so the gate
    takes no arcllm module-bus dependency; ``RegexPiiDetector`` is stateless and
    safe to instantiate per call.
    """
    from arcllm._pii import RegexPiiDetector, redact_text

    detector = RegexPiiDetector()
    matches = detector.detect(text)
    return str(redact_text(text, matches)) if matches else text


def redact_arguments(arguments: Mapping[str, object]) -> dict[str, str]:
    """Return a redacted, length-bounded per-argument preview for operator triage.

    Each value is stringified, PII/secret-redacted, then truncated to a small cap.
    Lets the operator see WHAT is being acted on (which file/URL/recipient/body)
    without leaking secrets or inflating the log (LLM02).
    """
    preview: dict[str, str] = {}
    for name, value in arguments.items():
        rendered = _redact(str(value))
        if len(rendered) > _MAX_ARG_VALUE_LEN:
            rendered = rendered[:_MAX_ARG_VALUE_LEN] + "..."
        preview[name] = rendered
    return preview


def summarize_arguments(arguments: Mapping[str, object]) -> str:
    """Return a one-line redacted, bounded argument summary for a provenance entry."""
    line = ", ".join(f"{name}={value}" for name, value in redact_arguments(arguments).items())
    return line[:_MAX_ARG_SUMMARY_LEN]


@dataclass(frozen=True)
class ApprovalRequest:
    """Agent-originated approval request (labeled as such — ASI09).

    Beyond the tool/legs/hash the gate needs, carries the triage context an
    operator needs to decide a trifecta block (SPEC-035 approval enrichment):
    ``arguments`` (redacted preview of WHAT is being acted on), ``leg_provenance``
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


@dataclass
class HumanGateConfig:
    """Runtime config for the gate (mirrors the [tools.human_gate] TOML block)."""

    timeout_seconds: float = 300.0
    # Personal/enterprise only: leg-sets that may be auto-approved without a
    # human, e.g. [["private_data", "external_comms", "untrusted_input"]].
    auto_approve: list[frozenset[str]] = field(default_factory=list)


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
    ) -> None:
        self._agent_did = agent_did
        self._tier = tier
        self._config = config or HumanGateConfig()
        self._audit_sink = audit_sink
        self._channel = channel
        self._operator = OperatorApprovalAuthority(operator_signer)

    async def request(
        self,
        call: ToolCall,
        *,
        legs: frozenset[str],
        provenance: list[dict[str, object]] | None = None,
    ) -> ApprovalGrant | None:
        """Obtain a one-shot approval for ``call`` or return None (fail closed).

        ``legs`` is the accumulated forbidden union that tripped the gate — used
        for auto-approve matching and for labeling the request. ``provenance`` is
        the ordered list of prior calls that lit each leg (already redacted by the
        caller), threaded through so the operator can triage the composition.
        """
        from arctrust.policy import _hash_call

        request = ApprovalRequest(
            tool_name=call.tool_name,
            agent_did=call.agent_did,
            legs=legs,
            call_hash=_hash_call(call),
            arguments=redact_arguments(call.arguments),
            leg_provenance=provenance or [],
            session_id=call.session_id,
        )

        if self._auto_approvable(legs):
            self._emit("human_gate.auto_approved", request, outcome="auto_approve")
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

    def _auto_approvable(self, legs: frozenset[str]) -> bool:
        """Personal/enterprise may auto-approve named compositions; federal never.

        The named set must equal the tripping composition EXACTLY. A subset test
        would let a narrower entry (e.g. ``{private_data, external_comms}``)
        green-light a wider forbidden set — the tripping union is always a
        superset of any subset — silently authorizing more than the operator named.
        """
        if self._tier == "federal":
            return False
        return any(named == legs for named in self._config.auto_approve)

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

    def _emit(self, event: str, request: ApprovalRequest, *, outcome: str) -> None:
        if self._audit_sink is None:
            return
        try:
            self._audit_sink(
                event,
                {
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
    "redact_arguments",
    "summarize_arguments",
]
