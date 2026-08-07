"""Encryption at rest for WORM audit records (D-577).

SPEC-062 D-552 chose FULL capture: every connector call and response, inputs and
outputs, so an auditor can reconstruct what happened (NIST AU-3). The consequence
is that the audit chain now holds every email body and every document the fleet
reads, and anyone who can read the file can read all of it.

This module seals that content. Two design points earn their own note:

* **The content is sealed; the envelope is not.** Only ``extra`` — the field that
  carries captured arguments and responses — is encrypted. ``actor_did``,
  ``action``, ``target``, ``outcome`` and ``ts`` stay in the clear because they are
  the record's index: ``arcstore``'s WORM ingest and the arcui Security screen read
  exactly those five fields and never the content. Sealing them would blind the
  operational readers to buy nothing — the bodies are not in them. Reader-level
  access control over the envelope is SPEC-063's question, not this module's.
* **The seal sits under the hash, not over it.** :class:`~arctrust.audit.WormSink`
  seals before it hashes, so the chain commits to the ciphertext. Verification
  therefore needs no key at all: an auditor can prove the chain was not tampered
  with while remaining unable to read what it says, and a flipped byte inside the
  ciphertext still fails :func:`~arctrust.audit.verify_chain`.

Custody is deliberately absent. :func:`derive_record_key` turns key material the
deployment ALREADY custodies (the operator seed, see :mod:`arctrust.operator`) into
a distinct at-rest key, so nothing here introduces a second secret to store, rotate
or lose. Where that key should live, who rotates it, and whether an auditor key is
separate from an operator key are SPEC-063 decisions.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox

#: The single key a sealed ``extra`` carries. Dotted so it cannot collide with a
#: field name an ordinary emitter puts in ``extra``.
SEALED_KEY = "arc.audit.sealed"

#: Domain separation for :func:`derive_record_key`. The at-rest key must not be the
#: signing seed itself: one key, two algorithms is how key-reuse failures start.
_DERIVATION_PERSON = b"arc.audit.rest"

#: Seed length the derivation accepts — the Ed25519 seed size every Arc key uses.
_SEED_SIZE = 32


def derive_record_key(seed: bytes) -> bytes:
    """Derive the at-rest record key from custodied key material.

    Args:
        seed: 32 bytes the deployment already custodies — in practice the operator
            seed (:class:`~arctrust.operator.OperatorKey`), so encryption inherits
            that custody rather than adding a second secret.

    Returns:
        A 32-byte :class:`~nacl.secret.SecretBox` key, domain-separated from the
        seed so the same bytes are never used to both sign and encrypt.

    Raises:
        ValueError: ``seed`` is not exactly 32 bytes. Fail closed rather than
            derive a key from truncated material.
    """
    if len(seed) != _SEED_SIZE:
        raise ValueError(f"record key seed must be {_SEED_SIZE} bytes, got {len(seed)}")
    derived = hashlib.blake2b(seed, digest_size=SecretBox.KEY_SIZE, person=_DERIVATION_PERSON)
    return derived.digest()


def is_sealed(event: Mapping[str, Any]) -> bool:
    """Whether this event dump's captured content is encrypted."""
    extra = event.get("extra")
    return isinstance(extra, dict) and list(extra) == [SEALED_KEY]


class RecordCipher:
    """Seals and opens the captured content of one audit record.

    XSalsa20-Poly1305 (:class:`~nacl.secret.SecretBox`) — authenticated, so a
    modified ciphertext fails to open rather than decrypting to garbage, and a
    fresh random nonce per record so two identical captures do not look identical
    on disk.

    Args:
        key: A 32-byte key, normally from :func:`derive_record_key`.
    """

    def __init__(self, key: bytes) -> None:
        self._box = SecretBox(key)

    def seal(self, event: dict[str, Any]) -> dict[str, Any]:
        """Return ``event`` with its ``extra`` content replaced by ciphertext.

        Sealing is unconditional: a record with no content is sealed too, so the
        shape on disk never advertises which records carried a body.
        """
        raw = json.dumps(event.get("extra") or {}, default=str, sort_keys=True)
        blob = self._box.encrypt(raw.encode("utf-8"))
        return {**event, "extra": {SEALED_KEY: base64.b64encode(blob).decode("ascii")}}

    def unseal(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Return ``event`` with its captured content restored.

        An event that is not sealed is returned unchanged — a chain may legitimately
        mix sealed and unsealed records across a custody change.

        Raises:
            ValueError: The record is sealed but this key cannot open it, or the
                plaintext is not the JSON object that was sealed.
        """
        if not is_sealed(event):
            return dict(event)
        blob = base64.b64decode(str(event["extra"][SEALED_KEY]), validate=True)
        try:
            plaintext = self._box.decrypt(blob)
        except CryptoError as exc:
            raise ValueError("sealed audit record does not open under this key") from exc
        extra = json.loads(plaintext.decode("utf-8"))
        if not isinstance(extra, dict):
            raise ValueError("sealed audit content is not an object")
        return {**event, "extra": extra}


__all__ = ["SEALED_KEY", "RecordCipher", "derive_record_key", "is_sealed"]
