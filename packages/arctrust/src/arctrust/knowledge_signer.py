"""The signing capability of a connection's knowledge principal (alpha-2 D7).

A connection granted to several agents is synced once into a shared store that
no agent owns; its identity is ``did:arc:knowledge:<hash>``. That principal
signs the store's OKF seal (the ``index.md`` / ``log.md`` sidecars), so a reader
trusts the store's routing index only when it verifies against the principal's
public key, never an agent's key and never another connection's.

Custody follows the deployment, like every other key arctrust holds:

* ``in_process`` (personal, zero config): the key is derived from the operator
  seed with a keyed BLAKE2b PRF, personalised for this use and keyed by the
  principal DID. Deterministic, so an existing store keeps its key across
  restarts and every agent process on the host derives the same one, with no
  new file to provision. The same custody as connector credentials
  (:mod:`arctrust.connector_cipher`) and the audit at-rest key.
* ``vault_transit`` (enterprise / federal): one non-exportable transit key,
  :data:`KNOWLEDGE_KEY_REF`, signs by reference; the principal DID is bound into
  every signed seal, so connections stay separated. Nothing is minted here: an
  unprovisioned key answers ``None`` and the store's indexes stay unwritten
  (fail closed).

The returned :class:`KnowledgeSigner` exposes a ``sign`` capability and the
public half only; there is no accessor for private material.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from arctrust.operator import OperatorKeyIntegrityError
from arctrust.operator_resolver import machine_security, operator_key_for, operator_transit_for
from arctrust.paths import Base
from arctrust.signer import (
    VAULT_TRANSIT,
    InProcessSigner,
    Signer,
    SignerError,
    VaultSigner,
)

_logger = logging.getLogger("arctrust.knowledge_signer")

#: The transit key reference a ``vault_transit`` deployment provisions for seals.
KNOWLEDGE_KEY_REF = "knowledge"
#: What a knowledge principal DID looks like (``store_key`` is 32 lowercase hex).
_PRINCIPAL = re.compile(r"did:arc:knowledge:[0-9a-f]{32}")
#: BLAKE2b personalisation: the bytes that seal stores are never any other key.
_PERSON = b"arc-knowledge-k1"


class KnowledgeSigner:
    """A knowledge principal's signing capability; holds no exportable material."""

    __slots__ = ("__did", "__signer")

    def __init__(self, did: str, signer: Signer) -> None:
        self.__did = did
        self.__signer = signer

    @property
    def did(self) -> str:
        return self.__did

    @property
    def public_key(self) -> bytes:
        return self.__signer.public_key

    @property
    def algorithm(self) -> str:
        return self.__signer.algorithm

    @property
    def can_sign(self) -> bool:
        return True

    def sign(self, message: bytes) -> bytes:
        return self.__signer.sign(message)


def knowledge_signer_for(
    principal: str, security: Any = None, *, base: Base = None
) -> KnowledgeSigner | None:
    """The signing capability for ``principal`` under this deployment's custody.

    ``None`` when the deployment holds no key for it (no operator key in
    process, or an unprovisioned transit key): the caller then writes nothing a
    reader could be asked to trust.

    Raises:
        ValueError: ``principal`` is not a knowledge principal DID.
    """
    if _PRINCIPAL.fullmatch(principal) is None:
        raise ValueError("not a knowledge principal DID")
    sec = security if security is not None else machine_security(base)
    try:
        if sec.custody == VAULT_TRANSIT:
            transit = operator_transit_for(sec, base)
            return KnowledgeSigner(
                principal, VaultSigner(transit, KNOWLEDGE_KEY_REF, sec.signing_algorithm)
            )
        operator = operator_key_for(sec, base=base)
    except (OSError, SignerError, OperatorKeyIntegrityError) as exc:
        _logger.warning(
            "knowledge principal %s has no signing key under custody=%s (%s); "
            "its store's indexes stay unwritten",
            principal,
            sec.custody,
            type(exc).__name__,
        )
        return None
    if operator is None:  # pragma: no cover - operator_key_for answers None only for transit
        return None
    seed = hashlib.blake2b(
        principal.encode("ascii"), digest_size=32, key=operator.seed, person=_PERSON
    ).digest()
    return KnowledgeSigner(principal, InProcessSigner(seed, sec.signing_algorithm))


__all__ = ["KNOWLEDGE_KEY_REF", "KnowledgeSigner", "knowledge_signer_for"]
