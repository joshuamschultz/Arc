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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from arctrust.identity import did_from_public_key
from arctrust.policy import ApprovalGrant, ToolCall, sign_approval
from arctrust.signer import Signer

_logger = logging.getLogger("arcagent.human_gate")

# An approval channel surfaces the request to a human (arcteam human-channel/DM
# path, OQ-3) and returns True iff a human explicitly approved. Fail-closed on
# any exception/timeout is enforced by the gate, not the channel.
ApprovalChannel = Callable[["ApprovalRequest"], Awaitable[bool]]
AuditSink = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class ApprovalRequest:
    """Agent-originated approval request (labeled as such — ASI09)."""

    tool_name: str
    agent_did: str
    legs: frozenset[str]
    call_hash: str
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
        self._operator = _OperatorApprovalAuthority(operator_signer)

    async def request(self, call: ToolCall, *, legs: frozenset[str]) -> ApprovalGrant | None:
        """Obtain a one-shot approval for ``call`` or return None (fail closed).

        ``legs`` is the accumulated forbidden union that tripped the gate — used
        for auto-approve matching and for labeling the request.
        """
        from arctrust.policy import _hash_call

        request = ApprovalRequest(
            tool_name=call.tool_name,
            agent_did=call.agent_did,
            legs=legs,
            call_hash=_hash_call(call),
        )

        if self._auto_approvable(legs):
            self._emit("human_gate.auto_approved", request, outcome="auto_approve")
            return sign_approval(call, self._operator)

        if self._channel is None:
            self._emit("human_gate.denied", request, outcome="no_channel")
            return None

        approved = await self._ask_human(request)
        if not approved:
            self._emit("human_gate.denied", request, outcome="denied_or_timeout")
            return None

        self._emit("human_gate.granted", request, outcome="granted")
        return sign_approval(call, self._operator)

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

    async def _ask_human(self, request: ApprovalRequest) -> bool:
        """Surface the request to the human channel; fail closed on timeout/error."""
        channel = self._channel
        if channel is None:
            return False
        try:
            return await asyncio.wait_for(channel(request), timeout=self._config.timeout_seconds)
        except TimeoutError:
            return False
        except Exception:  # reason: fail-closed — any channel error denies
            _logger.exception("Approval channel raised; failing closed")
            return False

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
                    "outcome": outcome,
                    "origin": request.origin,
                    "tier": self._tier,
                },
            )
        except Exception:  # reason: fail-open — audit must not mask the decision
            _logger.exception("Human-gate audit sink raised; continuing")


@dataclass(frozen=True)
class _OperatorApprovalAuthority:
    """Adapt an operator :class:`~arctrust.signer.Signer` to the approval authority.

    Satisfies ``arctrust.policy.ApprovalAuthority`` (did + public_key + algorithm
    + sign). The DID is derived from the operator public key so
    ``did_matches_pubkey`` holds inside ``verify_approval`` and the approver DID
    is provably distinct from any agent DID (ASI09 self-approval guard) — for
    Ed25519 (in-process) and ECDSA-P256 (vault_transit/federal) alike.
    """

    _signer: Signer

    @property
    def did(self) -> str:
        return did_from_public_key(self._signer.public_key, org="operator", agent_type="approver")

    @property
    def public_key(self) -> bytes:
        return self._signer.public_key

    @property
    def algorithm(self) -> str:
        return self._signer.algorithm

    def sign(self, message: bytes) -> bytes:
        return self._signer.sign(message)


__all__ = [
    "ApprovalChannel",
    "ApprovalRequest",
    "HumanGate",
    "HumanGateConfig",
]
