"""SPEC-082 T-1104 — the door's fleet-enrollment gate.

A caller can present a valid, signed identity (:func:`~arcagent.modules.mcp_server.
identity.verify_inbound`) and still not be a *member of this fleet*. Holding a key
proves possession, not admission (ASI04): a foreign harness is untrusted code, so
"it signed correctly" grants it nothing. Admission is an operator-signed
:class:`arctrust.policy.EnrollmentGrant`, verified against the trust-store operator
key BEFORE the caller reaches the door; the verified members' DIDs arrive here as
the ``enrolled`` roster. This gate is the membership check standing between a
verified identity and dispatch.

Enrollment is the ADR-019 stringency dial, not a second trust model. Two rules,
both fail-closed:

- **Mandatory at enterprise/federal.** A ``None`` or empty roster there enrolls
  nobody, so every caller is refused until an operator enrolls one.
- **Configured-gate at personal.** Enrollment is *optional* at personal only while
  the roster is empty — then a self-signed caller is admitted (open). The moment an
  operator sets a non-empty ``enrolled`` roster, it is ENFORCED at personal too: a
  verified caller not on the roster is refused. This is how a personal door is
  protected without changing tier — list who may call and only they get in
  (NIST 800-53 AC-3, deny-by-default). An empty roster is never a silent allow at a
  tier that configured one.
"""

from __future__ import annotations

from collections.abc import Collection

from arctrust import AuditSink

from arcagent.modules.mcp_server.audit import emit_door_event
from arcagent.modules.mcp_server.identity import InboundRejected

#: Tiers at which fleet enrollment is mandatory (ADR-019 stringency dial).
_ENROLLMENT_REQUIRED_TIERS = frozenset({"enterprise", "federal"})

#: The verb recorded on an enrollment denial.
_ENROLLMENT_VERB = "enrollment"


def require_enrolled(
    caller_did: str,
    enrolled: Collection[str] | None,
    *,
    tier: str,
    audit_sink: AuditSink,
) -> None:
    """Refuse a verified-but-unenrolled caller when enrollment is in force.

    Enrollment is in force when the tier mandates it (enterprise/federal) OR the
    operator has configured a non-empty roster (any tier, personal included). When
    in force, only a DID on the roster is admitted; anyone else gets one ``deny``
    audit event through the door's single emission point and an
    :class:`InboundRejected` — fail-closed. A personal door with an empty roster is
    the only open case (enrollment optional), and it is a no-op here.
    """
    required = tier in _ENROLLMENT_REQUIRED_TIERS
    configured = bool(enrolled)
    if not required and not configured:
        return  # personal + no roster = open (enrollment optional)
    if enrolled and caller_did in enrolled:
        return
    emit_door_event(
        audit_sink,
        caller_did=caller_did,
        verb=_ENROLLMENT_VERB,
        outcome="deny",
        tier=tier,
        arguments={},
    )
    raise InboundRejected(
        f"caller {caller_did!r} is not enrolled in this fleet; tier {tier!r} "
        "requires an operator-signed enrollment (ASI04)"
    )


__all__ = ["require_enrolled"]
