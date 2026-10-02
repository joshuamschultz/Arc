"""Signed JSON side records for the shared-knowledge store (alpha-2 item 16).

Two kinds of record sit beside the signed documents, neither inside them (so a
document's digest and signature never change):

* **provenance** — who promoted a document and on what decision, signed by the
  contributing agent's key;
* **demotion tombstones** — an operator's demote, signed by the operator key.

A record is its JSON payload plus ``public_key`` (base64), ``algorithm`` and
``signature`` (hex) over the canonical payload. The signer DID named in the
payload must be derived from that public key, so a record cannot claim another
signer. Who the signer is ALLOWED to be (pinned contributor, anchored operator)
is the caller's check, not this module's.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Protocol

from arctrust import canonical_json, verify_signature
from arctrust.identity import did_matches_pubkey

_SIGNATURE_FIELDS = ("public_key", "algorithm", "signature")


class RecordSigner(Protocol):
    @property
    def public_key(self) -> bytes: ...

    @property
    def algorithm(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


def sign_record(payload: dict[str, Any], signer: RecordSigner) -> str:
    """The JSON text of ``payload`` signed by ``signer`` (one line, sorted keys)."""
    record = dict(payload)
    record["public_key"] = base64.b64encode(signer.public_key).decode("ascii")
    record["algorithm"] = signer.algorithm
    record["signature"] = signer.sign(canonical_json(payload)).hex()
    return json.dumps(record, sort_keys=True) + "\n"


def verified_payload(text: str, *, signer_field: str) -> tuple[dict[str, Any], bytes] | None:
    """The payload and signer public key of a valid record; ``None`` for anything else.

    Fails closed on malformed JSON, a missing field, a key that does not derive
    the DID in ``signer_field``, or a signature that does not verify.
    """
    try:
        record = json.loads(text)
        public_key = base64.b64decode(record["public_key"], validate=True)
        signature = bytes.fromhex(record["signature"])
        algorithm = str(record["algorithm"])
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(record, dict):
        return None
    payload = {key: value for key, value in record.items() if key not in _SIGNATURE_FIELDS}
    signer_did = payload.get(signer_field)
    if not isinstance(signer_did, str) or not did_matches_pubkey(signer_did, public_key):
        return None
    if not verify_signature(algorithm, canonical_json(payload), signature, public_key):
        return None
    return payload, public_key


__all__ = ["RecordSigner", "sign_record", "verified_payload"]
