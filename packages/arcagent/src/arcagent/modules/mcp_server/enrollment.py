"""SPEC-082 T-1104 — the door's fleet-enrollment gate.

A caller can present a valid, signed identity (:func:`~arcagent.modules.mcp_server.
identity.verify_inbound`) and still not be a *member of this fleet*. Holding a key
proves possession, not admission (ASI04): a foreign harness is untrusted code, so
"it signed correctly" grants it nothing. Admission is an operator-signed
:class:`arctrust.policy.EnrollmentGrant`, verified against the trust-store operator
key BEFORE the caller reaches the door; the verified members' DIDs arrive here as
the ``enrolled`` roster. This gate is the membership check standing between a
verified identity and dispatch.

Enrollment is the ADR-019 stringency dial, not a second trust model: it is
mandatory at enterprise/federal and optional at personal (a self-signed personal
caller is admitted without a roster). The check is fail-closed — a ``None`` or
empty roster enrolls nobody, so a tier that requires enrollment refuses every
caller until an operator enrolls one (NIST 800-53 AC-3, deny-by-default).
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
    """Refuse a verified-but-unenrolled caller at a tier that mandates enrollment.

    A no-op at personal tier (enrollment optional) and for an enrolled DID at any
    tier. Otherwise it emits one ``deny`` audit event through the door's single
    emission point and raises :class:`InboundRejected` — fail-closed: a ``None`` or
    empty roster enrolls nobody, so the caller is refused by default.
    """
    if tier not in _ENROLLMENT_REQUIRED_TIERS:
        return
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
