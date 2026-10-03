"""Seal connector credentials at rest under a key derived from the operator seed (P18-2).

Every connector credential Arc holds (an OAuth refresh token, an API token, a
short-lived access token) is stored as one sealed value in an arcstore custody
row. This module is the in-process cipher for that row.

Three properties are load-bearing:

* **AEAD with the coordinate as associated data.** XChaCha20-Poly1305 (IETF), and
  the authenticated data names the exact ``(scope, slot)`` a value belongs to. A
  ciphertext copied from connection B's row into connection A's row, or from the
  ``client_secret`` slot into the ``refresh_token`` slot, fails to open. Moving a
  sealed value is therefore not a way to make Arc use it somewhere else.
* **A random 24-byte nonce per seal.** XChaCha's extended nonce makes random
  nonces safe at any volume, so two seals of one value never look alike on disk.
* **Custody is inherited, not added.** The key is ``blake2b`` of the operator seed
  with its own personalization, the same derivation and custody the audit at-rest
  key uses (:mod:`arctrust.audit_cipher`), domain-separated so the bytes that sign
  are never the bytes that encrypt. The derived key is held privately and has no
  accessor.

XChaCha20-Poly1305 is not a FIPS-approved algorithm, so a deployment that requires
FIPS refuses this cipher and must hold connector credentials under Vault Transit.

:class:`TransitConnectorCipher` is the ``vault_transit`` row cipher (P18-2F): the
same ``seal``/``open`` contract and the same associated data, but every value is
encrypted by reference in the transit under a non-exportable AES-256-GCM key, so
the custody key never enters this process. A transit that cannot answer raises
:class:`CredentialCustodyUnavailableError`, never a plaintext or in-process
fallback.

Errors never carry plaintext or ciphertext.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from typing import ClassVar, Literal

from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
    crypto_aead_xchacha20poly1305_ietf_KEYBYTES,
    crypto_aead_xchacha20poly1305_ietf_NPUBBYTES,
)
from nacl.exceptions import CryptoError

from arctrust.fips import assert_fips_if_required
from arctrust.operator import OperatorKey
from arctrust.transit_cipher import TransitCipher, TransitDecryptError, TransitUnavailableError

#: Version + cipher tag every sealed value starts with.
SEALED_PREFIX = "v1.xc."
#: The same for values sealed by the transit (P18-2F).
TRANSIT_PREFIX = "v1.tr."

#: Largest plaintext accepted; matches the old env-file value cap.
MAX_PLAINTEXT_BYTES = 64 * 1024

_AAD_PREFIX = b"arc:connector-credential:v1:"
_SEED_SIZE = 32
_ALGORITHM = "xchacha20-poly1305"
#: A connection instance, a field name, or ``app_<provider>`` for an OAuth app slot.
_COORDINATE = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")


class CredentialSealError(ValueError):
    """A credential could not be sealed or opened. The message names no material."""


class CredentialCustodyUnavailableError(RuntimeError):
    """The custody transit cannot answer; the credential is locked, not lost.

    Deliberately NOT a :class:`CredentialSealError`: an unreachable vault says
    nothing about the stored value, so it must never be reported as "unreadable,
    reconnect" (which would have the operator destroy a good credential).
    """


def _derive_connector_key(seed: bytes) -> bytes:
    """The custody key for connector credentials, derived from the operator seed."""
    if len(seed) != _SEED_SIZE:
        raise CredentialSealError("operator seed has the wrong length")
    return hashlib.blake2b(
        seed,
        digest_size=crypto_aead_xchacha20poly1305_ietf_KEYBYTES,
        person=ConnectorSecretCipher.PERSON,
    ).digest()


def _aad(scope: str, slot: str) -> bytes:
    for name, value in (("scope", scope), ("slot", slot)):
        if _COORDINATE.fullmatch(value) is None:
            raise CredentialSealError(f"credential {name} is not a valid coordinate")
    return _AAD_PREFIX + scope.encode("ascii") + b":" + slot.encode("ascii")


class ConnectorSecretCipher:
    """Seal/open connector credentials under a key derived from the operator seed.

    Holds the derived key privately; exposes no accessor for it.
    """

    PERSON: ClassVar[bytes] = b"arc-conn-vault1"

    __slots__ = ("__key",)

    def __init__(self, key: bytes) -> None:
        if len(key) != crypto_aead_xchacha20poly1305_ietf_KEYBYTES:
            raise CredentialSealError("connector cipher key has the wrong length")
        self.__key = key

    @classmethod
    def for_operator_key(
        cls, key: OperatorKey, *, require_fips: bool = False
    ) -> ConnectorSecretCipher:
        """The cipher for a deployment whose operator seed is in this process.

        Raises:
            ArcTrustFipsError: ``require_fips`` is set; XChaCha20 is not approved,
                so FIPS deployments hold connector credentials under Vault Transit.
        """
        if require_fips:
            assert_fips_if_required(require_fips=True, algorithm=_ALGORITHM)
        return cls(_derive_connector_key(key.seed))

    @property
    def kind(self) -> Literal["xc1"]:
        """Recorded on every custody row so a row sealed by another cipher is refused."""
        return "xc1"

    def seal(self, plaintext: bytes, *, scope: str, slot: str) -> str:
        """Encrypt one value bound to ``(scope, slot)``."""
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise CredentialSealError("credential value is too large to seal")
        aad = _aad(scope, slot)
        nonce = os.urandom(crypto_aead_xchacha20poly1305_ietf_NPUBBYTES)
        body = crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, aad, nonce, self.__key)
        encoded = base64.urlsafe_b64encode(nonce + body).decode("ascii").rstrip("=")
        return SEALED_PREFIX + encoded

    def open(self, sealed: str, *, scope: str, slot: str) -> bytes:
        """Decrypt one value, refusing anything not sealed for exactly ``(scope, slot)``.

        Raises:
            CredentialSealError: wrong prefix, malformed encoding, wrong key, a
                tampered byte, or a value sealed for another coordinate.
        """
        aad = _aad(scope, slot)
        if not sealed.startswith(SEALED_PREFIX):
            raise CredentialSealError("sealed credential was not made by this cipher")
        body = sealed[len(SEALED_PREFIX) :]
        try:
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        except (binascii.Error, ValueError):
            raise CredentialSealError("sealed credential is not well-formed") from None
        nonce_size = crypto_aead_xchacha20poly1305_ietf_NPUBBYTES
        if len(raw) <= nonce_size:
            raise CredentialSealError("sealed credential is not well-formed")
        try:
            return crypto_aead_xchacha20poly1305_ietf_decrypt(
                raw[nonce_size:], aad, raw[:nonce_size], self.__key
            )
        except CryptoError:
            raise CredentialSealError(
                "sealed credential does not open for this coordinate under this key"
            ) from None


class TransitConnectorCipher:
    """Seal/open connector credentials by reference in the custody transit.

    AES-256-GCM (FIPS-approved) under the transit's key ``KEY_REF``; associated
    data is exactly the in-process cipher's, so the ``(scope, slot)`` binding is
    identical at every tier. Holds only the transit handle and the key name.
    """

    KEY_REF: ClassVar[str] = "connector-credentials"
    _ALGORITHM: ClassVar[str] = "aes-256-gcm"

    __slots__ = ("_key_ref", "_transit")

    def __init__(
        self, transit: TransitCipher, *, key_ref: str = KEY_REF, require_fips: bool = False
    ) -> None:
        if require_fips:
            assert_fips_if_required(require_fips=True, algorithm=self._ALGORITHM)
        self._transit = transit
        self._key_ref = key_ref

    @property
    def kind(self) -> Literal["transit1"]:
        """Recorded on every custody row so a row sealed by another cipher is refused."""
        return "transit1"

    def seal(self, plaintext: bytes, *, scope: str, slot: str) -> str:
        """Encrypt one value bound to ``(scope, slot)`` in the transit.

        Raises:
            CredentialSealError: bad coordinate or oversize value.
            CredentialCustodyUnavailableError: the transit cannot answer.
        """
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise CredentialSealError("credential value is too large to seal")
        aad = _aad(scope, slot)
        try:
            return TRANSIT_PREFIX + self._transit.encrypt(self._key_ref, plaintext, aad=aad)
        except TransitUnavailableError:
            raise CredentialCustodyUnavailableError(
                "the credential custody transit cannot seal right now"
            ) from None

    def open(self, sealed: str, *, scope: str, slot: str) -> bytes:
        """Decrypt one value, refusing anything not sealed for exactly ``(scope, slot)``.

        Raises:
            CredentialSealError: wrong prefix, malformed, tampered, another key,
                or a value sealed for another coordinate.
            CredentialCustodyUnavailableError: the transit cannot answer.
        """
        aad = _aad(scope, slot)
        if not sealed.startswith(TRANSIT_PREFIX):
            raise CredentialSealError("sealed credential was not made by this cipher")
        try:
            return self._transit.decrypt(self._key_ref, sealed[len(TRANSIT_PREFIX) :], aad=aad)
        except TransitDecryptError:
            raise CredentialSealError(
                "sealed credential does not open for this coordinate under this key"
            ) from None
        except TransitUnavailableError:
            raise CredentialCustodyUnavailableError(
                "the credential custody transit cannot open right now"
            ) from None


__all__ = [
    "MAX_PLAINTEXT_BYTES",
    "SEALED_PREFIX",
    "TRANSIT_PREFIX",
    "ConnectorSecretCipher",
    "CredentialCustodyUnavailableError",
    "CredentialSealError",
    "TransitConnectorCipher",
]
