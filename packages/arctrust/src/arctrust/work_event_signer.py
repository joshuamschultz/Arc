"""The dedicated work-events signing key (SPEC-088 COMP-019, REQ-626, D-848).

Outbound work-event callbacks are signed so a receiver can prove they came from
this deployment. They are signed by a key minted for that one purpose, never by
the operator key: the operator key is the audit authority for every chain, and a
key whose signatures leave the box on every callback must not be able to forge
an audit record (or the other way round).

Custody is the deployment's custody transit at every tier, chosen by
:func:`arctrust.operator_resolver.custody_transit_for`: the local out-of-process
notary keystore when no ``[security.vault]`` is configured (personal, and
enterprise without Vault), HashiCorp Vault Transit otherwise. The key is created
non-exportable with the deployment ``signing_algorithm`` (Ed25519 by default,
ECDSA-P256 on a FIPS deployment) and signs by reference, so its seed never
enters this process.

Unlike :mod:`arctrust.knowledge_signer`, nothing is derived from the operator
seed. An unprovisioned key answers ``None`` and callbacks stay unsent (fail
closed); there is no fallback to the operator key or to any in-process seed.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from arctrust.audit import AuditEvent, emit
from arctrust.operator_resolver import custody_transit_for, machine_security
from arctrust.paths import Base
from arctrust.signer import Signer, SignerError, VaultSigner

_logger = logging.getLogger("arctrust.work_event_signer")

#: The custody key reference the work-events key is held under.
WORK_EVENTS_KEY_REF = "work-events"
_ACTOR_DID = "did:arc:system:work-events"


class WorkEventSigner:
    """The work-events signing capability; holds no exportable material.

    Public surface is exactly :meth:`sign`, :attr:`public_key`, :attr:`algorithm`
    and :attr:`key_id`. The wrapped signer signs by reference through the custody
    transit.
    """

    __slots__ = ("__signer",)

    def __init__(self, signer: Signer) -> None:
        self.__signer = signer

    @property
    def public_key(self) -> bytes:
        """The verify key receivers pin (raw Ed25519, or DER SPKI for ECDSA-P256)."""
        return self.__signer.public_key

    @property
    def algorithm(self) -> str:
        """``ed25519`` or ``ecdsa-p256``: the deployment signing algorithm."""
        return self.__signer.algorithm

    @property
    def key_id(self) -> str:
        """``sha256(public_key)`` first 16 hex chars; sent with every callback."""
        return hashlib.sha256(self.__signer.public_key).hexdigest()[:16]

    def sign(self, message: bytes) -> bytes:
        """Sign ``message`` by reference with the work-events key."""
        return self.__signer.sign(message)


class _LogSink:
    """Audit sink for setup-time provisioning: the structured log (no chain exists yet)."""

    def write(self, event: AuditEvent) -> None:
        _logger.info(
            "audit %s target=%s outcome=%s extra=%s",
            event.action,
            event.target,
            event.outcome,
            event.extra,
        )


def provision_work_event_key(security: Any = None, *, base: Base = None) -> bytes:
    """Mint the work-events key in the deployment's custody transit if absent.

    Idempotent: an existing key is left unchanged and its public key returned.
    The operator key is never read or touched. Emits one audit event.

    Raises:
        SignerError: the custody transit cannot create or serve the key
            (unreachable Vault, unconfigured ``work-events`` Vault key name, a key
            that exists but is exportable or of another type).
        OSError: the notary keystore cannot be written.
    """
    sec = security if security is not None else machine_security(base)
    outcome = "error"
    try:
        public_key = custody_transit_for(sec, base).create_signing_key(WORK_EVENTS_KEY_REF)
        outcome = "allow"
        return public_key
    finally:
        emit(
            AuditEvent(
                actor_did=_ACTOR_DID,
                action="work_event_key.provision",
                target=f"custody:{WORK_EVENTS_KEY_REF}",
                outcome=outcome,
                extra={"custody": sec.custody, "algorithm": sec.signing_algorithm},
            ),
            _LogSink(),
        )


def work_event_signer_for(security: Any = None, *, base: Base = None) -> WorkEventSigner | None:
    """The work-events signing capability, or ``None`` when the key is not provisioned.

    ``None`` also covers a custody transit that cannot serve the key; the caller
    then starts no callback dispatcher (fail closed). Never substitutes another key.
    """
    sec = security if security is not None else machine_security(base)
    try:
        transit = custody_transit_for(sec, base)
        signer = VaultSigner(transit, WORK_EVENTS_KEY_REF, sec.signing_algorithm)
    except (OSError, SignerError) as exc:
        _logger.warning(
            "no work-events signing key under custody=%s (%s); callbacks stay unsent",
            sec.custody,
            type(exc).__name__,
        )
        return None
    return WorkEventSigner(signer)


__all__ = [
    "WORK_EVENTS_KEY_REF",
    "WorkEventSigner",
    "provision_work_event_key",
    "work_event_signer_for",
]
