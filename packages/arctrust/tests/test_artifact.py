"""SPEC-033 A1 — detached artifact signature primitives.

arctrust owns content-hash + algorithm-dispatched detached-signature verify.
Higher layers sign agent-authored artifacts on write and re-verify them at load.

Signing resolves through :class:`arctrust.Signer`, so the same primitive covers
both custody models: a personal-tier seed held in this process, and a federal
vault/notary key that never enters it. Verification dispatches on the manifest's
recorded ``algorithm`` and fails closed on anything it does not support.
"""

from __future__ import annotations

from pathlib import Path

from arctrust.artifact import (
    ArtifactSignature,
    content_sha256,
    sign_artifact,
    sign_artifact_with_signer,
    verify_artifact,
)
from arctrust.keypair import generate_keypair
from arctrust.signer import (
    ECDSA_P256,
    ED25519,
    FileNotaryTransit,
    InProcessSigner,
    VaultSigner,
)


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


# ---------------------------------------------------------------------------
# Signer-based signing — the custody-agnostic path (SPEC-066 D-066-2)
# ---------------------------------------------------------------------------


def test_ed25519_signer_matches_the_seed_path() -> None:
    """The seed path is just an in-process Ed25519 signer — same manifest shape."""
    kp = generate_keypair()
    content = b"payload"
    manifest = sign_artifact_with_signer(
        content, signer_did="did:arc:a", signer=InProcessSigner(kp.private_key, ED25519)
    )
    assert manifest.algorithm == ED25519
    assert manifest.public_key == kp.public_key.hex()
    assert verify_artifact(content, manifest, trusted_public_key=kp.public_key) is True


def test_ecdsa_p256_signed_artifact_verifies() -> None:
    """The federal algorithm — impossible to sign or verify before D-066-2."""
    kp = generate_keypair()
    signer = InProcessSigner(kp.private_key, ECDSA_P256)
    content = b"async def fn(): return 42\n"

    manifest = sign_artifact_with_signer(content, signer_did="did:arc:fed", signer=signer)

    assert manifest.algorithm == ECDSA_P256
    assert manifest.public_key == signer.public_key.hex()
    assert verify_artifact(content, manifest) is True
    assert verify_artifact(content, manifest, trusted_public_key=signer.public_key) is True
    assert verify_artifact(b"tampered", manifest) is False


def test_vault_transit_signer_signs_without_the_seed_in_process(tmp_path: Path) -> None:
    """The federal custody path: the seed lives in the notary, never here.

    Uses the real :class:`FileNotaryTransit` — a fake transit would prove nothing
    about the out-of-process boundary a federal deployment actually runs.
    """
    seed = generate_keypair().private_key
    keystore = tmp_path / "notary"
    FileNotaryTransit.provision(keystore, "operator", seed, algorithm=ECDSA_P256)
    signer = VaultSigner(
        FileNotaryTransit(keystore, algorithm=ECDSA_P256), "operator", ECDSA_P256
    )
    content = b"vault-signed bytes"

    manifest = sign_artifact_with_signer(content, signer_did="did:arc:op", signer=signer)

    assert manifest.algorithm == ECDSA_P256
    assert verify_artifact(content, manifest, trusted_public_key=signer.public_key) is True


# ---------------------------------------------------------------------------
# Algorithm confusion — an attacker must not be able to pick the verifier
# ---------------------------------------------------------------------------


def test_relabelling_the_algorithm_refuses_an_otherwise_valid_signature() -> None:
    """The signature bytes and key are untouched; only ``algorithm`` is swapped.

    Both directions: an ECDSA manifest relabelled Ed25519 and an Ed25519 manifest
    relabelled ECDSA. Neither may verify — the recorded algorithm selects the
    verifier, so letting it drift lets an attacker choose the weaker check.
    """
    kp = generate_keypair()
    content = b"payload"

    ecdsa = sign_artifact_with_signer(
        content, signer_did="did:arc:a", signer=InProcessSigner(kp.private_key, ECDSA_P256)
    )
    assert verify_artifact(content, ecdsa.model_copy(update={"algorithm": ED25519})) is False

    ed = sign_artifact(content, signer_did="did:arc:a", private_key=kp.private_key)
    assert verify_artifact(content, ed.model_copy(update={"algorithm": ECDSA_P256})) is False


def test_unknown_or_empty_algorithm_never_verifies() -> None:
    """No algorithm value outside the supported set may skip verification.

    An empty string is called out because it is the shape a stripped field takes
    — it must fail closed, never fall back to the Ed25519 default.
    """
    kp = generate_keypair()
    content = b"payload"
    manifest = sign_artifact(content, signer_did="did:arc:a", private_key=kp.private_key)

    for algorithm in ("", "none", "rsa", "ED25519", "ed25519 "):
        relabelled = manifest.model_copy(update={"algorithm": algorithm})
        assert verify_artifact(content, relabelled) is False, algorithm


def test_pin_of_a_different_algorithm_key_fails_closed() -> None:
    """A pin is exact key bytes: the same seed's Ed25519 key must not satisfy an
    ECDSA-signed manifest, nor the reverse."""
    kp = generate_keypair()
    content = b"payload"
    ecdsa_signer = InProcessSigner(kp.private_key, ECDSA_P256)

    ecdsa = sign_artifact_with_signer(content, signer_did="did:arc:a", signer=ecdsa_signer)
    assert verify_artifact(content, ecdsa, trusted_public_key=kp.public_key) is False

    ed = sign_artifact(content, signer_did="did:arc:a", private_key=kp.private_key)
    assert verify_artifact(content, ed, trusted_public_key=ecdsa_signer.public_key) is False
