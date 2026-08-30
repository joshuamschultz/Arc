"""Fail-closed enrollment admission — the three chokepoints (H-040 §3.4).

A native ``arcagent`` is trusted by its self-consistent DID. A foreign harness is
untrusted code (ASI04): it may route, run, or render ONLY if it carries an
operator-signed :class:`~arctrust.policy.EnrollmentGrant` that verifies against
the **trust-store operator key** — never a key the member supplies (the
self-blessing trap). Verification is re-run at three chokepoints, each
fail-closed:

1. **Registry admission** — a foreign ``Entity`` with an absent or unverifiable
   grant is refused a registry row (:func:`admit_registration`).
2. **Roster eligibility** — a member reaches a routing decision only if it is
   ``active`` and its enrollment verifies (:func:`is_eligible`).
3. **Dispatch / start** — the fleet refuses to construct a live member for an
   entity that fails verification (:func:`guard_dispatch`), so forged code never
   runs even if a registry row was tampered in.

arctrust is a leaf and cannot see arcteam's ``Entity``, so this module extracts
the entity facts and calls :func:`arctrust.policy.verify_enrollment` with them —
keeping the crypto in arctrust and the entity shape in arcteam.
"""

from __future__ import annotations

from collections.abc import Callable

from arctrust.policy import enrollment_from_wire, verify_enrollment
from arctrust.trust_store import load_operator_pubkey

from arcteam.harness.trust import is_trusted
from arcteam.types import Entity, EntityStatus, EntityType

#: Resolve an operator DID to its trust-store Ed25519 pubkey. The default reads
#: ``~/.arc/trust/operators.toml``; tests inject a fake. The KEY POINT: the
#: operator key comes from the trust store, keyed by the grant's ``approver_did``
#: — never from the grant's own ``approver_public_key`` field (§3.4).
OperatorKeyResolver = Callable[[str], bytes]


class EnrollmentDenied(Exception):
    """A foreign member failed enrollment verification at a chokepoint — fail-closed."""


def default_operator_key_resolver(approver_did: str) -> bytes:
    """The production resolver: the operator pubkey from the arctrust trust store."""
    return load_operator_pubkey(approver_did)


def member_admitted(entity: Entity, resolve_operator_key: OperatorKeyResolver) -> bool:
    """Whether this member may be trusted as a fleet member — fail-closed.

    Native ``arcagent`` is trusted by identity (its DID is self-consistent). A
    foreign harness is trusted only if it carries an operator-signed grant that
    verifies against the trust-store operator key resolved from the grant's
    ``approver_did``. Every error (missing grant, unknown operator, malformed
    key, bad signature) resolves to False.
    """
    if is_trusted(entity.harness):
        return True
    grant_wire = entity.enrollment
    if grant_wire is None:
        return False
    try:
        grant = enrollment_from_wire(grant_wire)
        operator_key = resolve_operator_key(grant.approver_did)
        member_key = bytes.fromhex(entity.public_key)
    except Exception:  # reason: fail-closed — any admission error denies (ASI04)
        return False
    return verify_enrollment(
        grant,
        did=entity.did,
        handle=entity.handle,
        harness=entity.harness,
        member_public_key=member_key,
        operator_public_key=operator_key,
    )


def admit_registration(
    entity: Entity,
    *,
    resolve_operator_key: OperatorKeyResolver = default_operator_key_resolver,
) -> None:
    """Chokepoint 1 — refuse to register a foreign member that does not verify.

    Raises :class:`EnrollmentDenied` fail-closed. Native agents and users pass
    unchanged (native trust story preserved, §11).
    """
    if not member_admitted(entity, resolve_operator_key):
        raise EnrollmentDenied(
            f"foreign harness {entity.harness!r} for {entity.handle!r} is not admitted: "
            "absent or unverifiable operator enrollment grant"
        )


def is_eligible(
    entity: Entity,
    *,
    resolve_operator_key: OperatorKeyResolver = default_operator_key_resolver,
) -> bool:
    """Chokepoint 2 — whether a member may currently be given work.

    Eligible iff it is an active agent AND its enrollment verifies. A revoked or
    suspended member, or a foreign member whose grant no longer verifies, never
    reaches a routing decision.
    """
    if entity.type != EntityType.AGENT:
        return False
    if entity.status != EntityStatus.active:
        return False
    return member_admitted(entity, resolve_operator_key)


def guard_dispatch(
    entity: Entity,
    *,
    resolve_operator_key: OperatorKeyResolver = default_operator_key_resolver,
) -> None:
    """Chokepoint 3 — refuse to construct/start a live member that does not verify.

    Raises :class:`EnrollmentDenied` fail-closed. A tampered registry row (e.g. a
    swapped ``public_key``) breaks the pinned-key check here, so forged code never
    runs even if it slipped a row past admission.
    """
    if entity.status != EntityStatus.active:
        raise EnrollmentDenied(
            f"member {entity.handle!r} is {entity.status.value}, not active — refusing to dispatch"
        )
    if not member_admitted(entity, resolve_operator_key):
        raise EnrollmentDenied(
            f"member {entity.handle!r} failed enrollment verification at dispatch — refusing to run"
        )


__all__ = [
    "EnrollmentDenied",
    "OperatorKeyResolver",
    "admit_registration",
    "default_operator_key_resolver",
    "guard_dispatch",
    "is_eligible",
    "member_admitted",
]
