"""SPEC-033 A1 — detached artifact signature primitives.

arctrust owns content-hash + Ed25519 detached-signature verify. Higher layers
sign agent-authored artifacts on write and re-verify them at load.
"""

from __future__ import annotations

from arctrust.artifact import (
    ArtifactSignature,
    content_sha256,
    sign_artifact,
    verify_artifact,
)
from arctrust.keypair import generate_keypair


def test_sign_then_verify_roundtrip() -> None:
    kp = generate_keypair()
    content = b"async def fn(): return 42\n"
    manifest = sign_artifact(content, signer_did="did:arc:test", private_key=kp.private_key)
    assert manifest.signer_did == "did:arc:test"
    assert manifest.artifact_sha256 == content_sha256(content)
    assert verify_artifact(content, manifest) is True


def test_tamper_after_sign_fails_verify() -> None:
    kp = generate_keypair()
    manifest = sign_artifact(b"original", signer_did="did:arc:test", private_key=kp.private_key)
    assert verify_artifact(b"tampered", manifest) is False


def test_verify_with_trusted_pubkey_pin() -> None:
    kp = generate_keypair()
    other = generate_keypair()
    content = b"payload"
    manifest = sign_artifact(content, signer_did="did:arc:a", private_key=kp.private_key)
    assert verify_artifact(content, manifest, trusted_public_key=kp.public_key) is True
    # A valid self-signature from an untrusted key must NOT satisfy a pin.
    assert verify_artifact(content, manifest, trusted_public_key=other.public_key) is False


def test_sidecar_json_roundtrip() -> None:
    kp = generate_keypair()
    content = b"payload"
    manifest = sign_artifact(content, signer_did="did:arc:a", private_key=kp.private_key)
    restored = ArtifactSignature.from_json(manifest.to_json())
    assert restored == manifest
    assert verify_artifact(content, restored) is True


def test_forged_signature_fails() -> None:
    kp = generate_keypair()
    content = b"payload"
    manifest = sign_artifact(content, signer_did="did:arc:a", private_key=kp.private_key)
    forged = manifest.model_copy(update={"signature": "00" * 64})
    assert verify_artifact(content, forged) is False


def test_malformed_signature_hex_is_false_not_raise() -> None:
    kp = generate_keypair()
    content = b"payload"
    manifest = sign_artifact(content, signer_did="did:arc:a", private_key=kp.private_key)
    bad = manifest.model_copy(update={"signature": "not-hex"})
    assert verify_artifact(content, bad) is False
