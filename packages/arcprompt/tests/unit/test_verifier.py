"""COMP-003 / REQ-129: SignatureVerifier with mandatory pinning, no bypass."""

from __future__ import annotations

import os

from arctrust.artifact import sign_artifact
from arctrust.keypair import KeyPair
from packages.arcprompt.tests.conftest import SigningKey

from arcprompt.verifier import SignatureVerifier


def test_valid_signature_against_pinned_key_verifies(signer: SigningKey) -> None:
    content = b"overlay bytes"
    manifest = sign_artifact(content, signer_did=signer.did, private_key=signer.seed)
    assert SignatureVerifier(signer.public_key).verify(content, manifest) is True


def test_none_pinned_key_is_refused_not_skipped(signer: SigningKey) -> None:
    """An unpinned floor is no floor — a valid self-signature must NOT pass (T-738)."""
    content = b"overlay bytes"
    manifest = sign_artifact(content, signer_did=signer.did, private_key=signer.seed)
    assert SignatureVerifier(None).verify(content, manifest) is False


def test_wrong_key_is_rejected(signer: SigningKey) -> None:
    content = b"overlay bytes"
    manifest = sign_artifact(content, signer_did=signer.did, private_key=signer.seed)
    attacker_pub = KeyPair.from_seed(os.urandom(32)).public_key
    assert SignatureVerifier(attacker_pub).verify(content, manifest) is False


def test_tampered_content_is_rejected(signer: SigningKey) -> None:
    manifest = sign_artifact(b"original", signer_did=signer.did, private_key=signer.seed)
    assert SignatureVerifier(signer.public_key).verify(b"tampered", manifest) is False
