"""SPEC-062 COMP-013 — ``ApprovalBinding``, the per-instance human-approval setting.

The default is asymmetric on purpose (REQ-274): pulling data IN is free, pushing
anything OUT waits for a human. A poisoned page or mail an agent merely *reads*
costs nothing until the agent acts on it, and the acting is the only step worth a
human's attention. An operator who trusts one connected account relaxes it for
that account alone — ``mode`` is per instance, never per extension and never
global.

Two things this component does NOT do, because doing them would be a second,
weaker trust path:

* **It does not decide what an approval is.** A gated call goes to
  :class:`~arcagent.tools.human_gate.HumanGate`, which already owns the timeout,
  the fail-closed default, the one-shot grant bound to this exact call, and the
  pin to the deployment operator's DID. Approval is a signed grant an operator
  minted on a mechanical surface (``arc approve`` / the arcui operator action),
  never an answer in agent chat, which a prompt-injected message could forge
  (ASI09).
* **It does not re-derive the trifecta.** ``capability_tags`` travel with the
  tool from the manifest through :class:`~arcagent.extension.bridge.CapabilityBridge`,
  and :func:`~arcagent.core.session_internal.capability_ledger.legs_for_call`
  resolves them — including the owner-channel exemption, so delivering a result
  to the operator's own sink is not treated as egress.

**How the request names what it is asking about.** REQ-275 requires the presented
request to name the instance and the outbound target: *"sales wants to email
new@stranger.com"* is a decision an operator can make, *"sales wants to call
send_mail"* is not. The instance rides the qualified ``tool_name`` and the target
rides the arguments, which ``HumanGate`` presents as it received them. The *grant*
binds to the real recipient regardless, because the call hash covers the arguments.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.policy import ApprovalGrant, ToolCall

from arcagent.core.session_internal.capability_ledger import (
    EXTERNAL_COMMS,
    current_session_id,
    legs_for_call,
)
from arcagent.extension.attachment import (
    ExtensionAttachment,
    ProbeResult,
    Requirement,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.tools.human_gate import HumanGate

_logger = logging.getLogger("arcagent.extension.approval")

#: ``none`` — the operator has relaxed this connected account; ``outbound`` — the
#: default, inbound reads free and every outbound call gated; ``all`` — every call
#: gated, including reads.
ApprovalMode = Literal["none", "outbound", "all"]

_MODES: frozenset[str] = frozenset(get_args(ApprovalMode))

#: What an argument value has to look like to be an outbound destination. Shape,
#: not name: an extension picks its own argument names and core may not learn any
#: of them (REQ-280), so a list of vendor-blessed keys would be both a coupling
#: and a hole the first time an extension called the field ``recipient``.
_TARGET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[^\s@,;<>\"']+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"']+"),
)

#: The restrictive spec an undeclared tool is judged as. An upstream that serves a
#: verb the manifest never declared must not buy the free-read path by omission.
_UNDECLARED = ToolSpec(name="", classification="state_modifying")


def outbound_target(arguments: Mapping[str, Any]) -> str:
    """Return the first destination this call names, or ``""`` if it names none.

    Args:
        arguments: The call's real, unredacted arguments.

    Returns:
        An address the operator can recognise — a mail recipient, a URL — or the
        empty string, which is itself informative: this call sends nothing to a
        named external destination.
    """
    for value in arguments.values():
        text = str(value)
        for pattern in _TARGET_PATTERNS:
            found = pattern.search(text)
            if found is not None:
                return found.group(0)
    return ""


@dataclass(frozen=True)
class ApprovalDecision:
    """Whether this call may proceed, and the grant that admitted it.

    ``allowed`` and ``grant`` are deliberately separate: a call the mode never
    gated is allowed with no grant, and conflating the two into ``grant is None``
    would make "nobody asked" indistinguishable from "the operator said no".
    """

    allowed: bool
    reason: str
    grant: ApprovalGrant | None = None


class ApprovalBinding:
    """Binds one connected instance's approval mode onto the human gate.

    Args:
        instance: The connected account this binding governs — the name the
            operator sees in the request, and the unit ``mode`` applies to.
        agent_did: The agent whose call is being authorized.
        gate: The deployment's human gate. It owns the timeout, the fail-closed
            default, and the operator-DID pin; this class owns none of them.
        mode: The per-instance setting. Defaults to the asymmetric ``outbound``.
        audit_sink: Where approval verdicts are recorded, through the single
            :func:`~arctrust.audit.emit` chokepoint (CON-5).

    Raises:
        ValueError: ``mode`` is not one of :data:`ApprovalMode`. Refused at
            construction rather than treated as unknown-so-strict at call time,
            because a typo in an operator's config should be loud once, not a
            silently tightened connection nobody can explain.
    """

    def __init__(
        self,
        *,
        instance: str,
        agent_did: str,
        gate: HumanGate,
        mode: str = "outbound",
        audit_sink: AuditSink | None = None,
    ) -> None:
        if mode not in _MODES:
            raise ValueError(
                f"unknown approval mode {mode!r} for instance {instance!r}; "
                f"expected one of {sorted(_MODES)}"
            )
        self._instance = instance
        self._agent_did = agent_did
        self._gate = gate
        self._mode = mode
        self._sink = audit_sink

    @property
    def instance(self) -> str:
        """The connected account this binding governs."""
        return self._instance

    @property
    def mode(self) -> str:
        """The per-instance approval setting in force."""
        return self._mode

    def requires_approval(self, spec: ToolSpec, arguments: Mapping[str, Any]) -> bool:
        """Whether this call must wait for a human under the effective mode."""
        if self._mode == "none":
            return False
        if self._mode == "all":
            return True
        return self._is_outbound(spec, arguments)

    async def authorize(self, spec: ToolSpec, arguments: Mapping[str, Any]) -> ApprovalDecision:
        """Obtain permission for one call, or refuse it.

        Args:
            spec: The tool being called, carrying the classification and tags the
                manifest declared for it.
            arguments: The call's real arguments. Used unredacted, so the grant
                binds to the actual destination.

        Returns:
            The verdict. ``allowed`` is False whenever a human was needed and did
            not grant — denial, timeout, no channel, or a grant that failed the
            operator pin. Every one of those is the gate's judgement, not this
            class's.
        """
        if not self.requires_approval(spec, arguments):
            return ApprovalDecision(allowed=True, reason="not_required")

        target = outbound_target(arguments)
        legs = self._legs(spec, arguments)
        grant = await self._gate.request(self._subject(spec, arguments, legs), legs=legs)
        if grant is None:
            self._audit(spec, target, action="connector.approval_denied", outcome="deny")
            return ApprovalDecision(allowed=False, reason="approval_denied")

        self._audit(spec, target, action="connector.approval_granted", outcome="allow")
        return ApprovalDecision(allowed=True, reason="approval_granted", grant=grant)

    def bind(
        self, attachment: ExtensionAttachment, specs: Iterable[ToolSpec]
    ) -> ExtensionAttachment:
        """Wrap ``attachment`` so a gated call cannot reach it without a grant.

        Args:
            attachment: The live connection.
            specs: The tools it offers, as the bridge registered them. A call to
                a name absent from this list is judged by :data:`_UNDECLARED`.

        Returns:
            An attachment satisfying the same hook, whose ``invoke`` gates first.
        """
        return _GatedAttachment(self, attachment, specs)

    # --- internals ----------------------------------------------------------

    def _is_outbound(self, spec: ToolSpec, arguments: Mapping[str, Any]) -> bool:
        """Whether this call pushes something out rather than pulling something in.

        Two independent tests, because either alone leaks. Classification alone
        admits a ``read_only`` verb that publishes; the egress leg alone admits a
        ``state_modifying`` verb that mutates the remote system without any tag
        resolving to ``external_comms``.
        """
        if spec.classification == "state_modifying":
            return True
        return EXTERNAL_COMMS in self._legs(spec, arguments)

    def _legs(self, spec: ToolSpec, arguments: Mapping[str, Any]) -> frozenset[str]:
        """This call's trifecta legs, resolved by the one function that owns them."""
        return legs_for_call(spec.name, spec.capability_tags, arguments)

    def _qualified(self, spec: ToolSpec) -> str:
        """The verb as the operator should read it: which account, which action."""
        return f"{self._instance}.{spec.name}"

    def _subject(
        self, spec: ToolSpec, arguments: Mapping[str, Any], legs: frozenset[str]
    ) -> ToolCall:
        """The call the grant binds to — real arguments, so the target is covered."""
        return ToolCall(
            tool_name=self._qualified(spec),
            arguments=dict(arguments),
            agent_did=self._agent_did,
            session_id=current_session_id(),
            classification="unclassified",
            capability_tags=legs,
        )

    def _audit(self, spec: ToolSpec, target: str, *, action: str, outcome: str) -> None:
        """Record the verdict through the single emission chokepoint."""
        if self._sink is None:
            return
        emit(
            AuditEvent(
                actor_did=self._agent_did,
                action=action,
                target=f"connector:{self._qualified(spec)}",
                outcome=outcome,
                extra={
                    "instance": self._instance,
                    "tool": spec.name,
                    "outbound_target": target,
                    "mode": self._mode,
                    "classification": spec.classification,
                },
            ),
            self._sink,
        )


class _GatedAttachment:
    """One attachment with its outbound calls behind :class:`ApprovalBinding`.

    A whole :class:`~arcagent.extension.attachment.ExtensionAttachment`, not a
    partial one: whatever holds this holds an attachment, and a wrapper that gated
    ``invoke`` while dropping ``probe`` would break its holder in a way no test of
    the gating itself would catch.
    """

    def __init__(
        self,
        binding: ApprovalBinding,
        attachment: ExtensionAttachment,
        specs: Iterable[ToolSpec],
    ) -> None:
        self._binding = binding
        self._attachment = attachment
        self._specs: dict[str, ToolSpec] = {spec.name: spec for spec in specs}

    def requirements(self) -> list[Requirement]:
        return self._attachment.requirements()

    async def probe(self) -> ProbeResult:
        return await self._attachment.probe()

    async def describe_tools(self) -> list[ToolSpec]:
        return await self._attachment.describe_tools()

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Authorize, then call — never the other way round.

        A refusal returns a result the agent can read rather than raising: the
        operator declined this action, which is an answer, not a fault. The
        outcome is ``ERROR`` so it can never be mistaken for a completed send.
        """
        spec = self._specs.get(tool) or _UNDECLARED.model_copy(update={"name": tool})
        decision = await self._binding.authorize(spec, args)
        if not decision.allowed:
            _logger.info("connector call %r refused: %s", tool, decision.reason)
            return ToolResult(
                tool=tool,
                outcome=ToolOutcome.ERROR,
                content=(
                    f"instance {self._binding.instance!r} requires operator approval "
                    f"for this call and did not receive it: {decision.reason}"
                ),
            )
        return await self._attachment.invoke(tool, args)


__all__ = [
    "ApprovalBinding",
    "ApprovalDecision",
    "ApprovalMode",
    "outbound_target",
]
