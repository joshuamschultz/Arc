"""SPEC-037 — the arctrust ``Signer`` seam (asymmetric + out-of-process custody).

Covers REQ-005 (Signer Protocol + in-process impl), REQ-006 (vault-transit
by-reference signing — the seed never enters the process), REQ-007 (one config
seam selects the impl, fail-closed on a missing transit client).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arctrust import keypair
from arctrust.signer import (
    ECDSA_P256,
    ED25519,
    FileNotaryTransit,
    InProcessSigner,
    SignerConfig,
    SignerError,
    VaultSigner,
    build_signer,
    verify_signature,
)

# ---------------------------------------------------------------------------
# InProcessSigner — Ed25519 (REQ-005, default)
# ---------------------------------------------------------------------------


class TestInProcessSignerEd25519:
    def test_signature_verifies_with_public_key(self) -> None:
        seed = keypair.generate_keypair().private_key
        signer = InProcessSigner(seed)
        message = b"attest-this-payload"
        signature = signer.sign(message)
        assert signer.algorithm == ED25519
        assert keypair.verify(message, signature, signer.public_key)

    def test_verify_signature_dispatch_ed25519(self) -> None:
        seed = keypair.generate_keypair().private_key
        signer = InProcessSigner(seed)
        message = b"dispatch"
        signature = signer.sign(message)
        assert verify_signature(ED25519, message, signature, signer.public_key)
        # A different message must fail.
        assert not verify_signature(ED25519, b"other", signature, signer.public_key)

    def test_public_key_matches_seed(self) -> None:
        seed = keypair.generate_keypair().private_key
        signer = InProcessSigner(seed)
        assert signer.public_key == keypair.KeyPair.from_seed(seed).public_key


# ---------------------------------------------------------------------------
# InProcessSigner — ECDSA-P256 (REQ-004, the FIPS/federal path)
# ---------------------------------------------------------------------------


class TestInProcessSignerEcdsa:
    def test_ecdsa_signature_verifies_with_public_key(self) -> None:
        seed = keypair.generate_keypair().private_key
        signer = InProcessSigner(seed, algorithm=ECDSA_P256)
        message = b"federal-grade-payload"
        signature = signer.sign(message)
        assert signer.algorithm == ECDSA_P256
        assert verify_signature(ECDSA_P256, message, signature, signer.public_key)

    def test_ecdsa_verify_only_with_public_key(self) -> None:
        """The verifier holds ONLY the public key — no private material."""
        seed = keypair.generate_keypair().private_key
        signer = InProcessSigner(seed, algorithm=ECDSA_P256)
        message = b"non-repudiation"
        signature = signer.sign(message)
        public_key = signer.public_key
        del signer
        assert verify_signature(ECDSA_P256, message, signature, public_key)
        assert not verify_signature(ECDSA_P256, b"tampered", signature, public_key)

    def test_ecdsa_deterministic_public_key_from_seed(self) -> None:
        seed = keypair.generate_keypair().private_key
        a = InProcessSigner(seed, algorithm=ECDSA_P256)
        b = InProcessSigner(seed, algorithm=ECDSA_P256)
        assert a.public_key == b.public_key

    def test_unsupported_algorithm_rejected(self) -> None:
        seed = keypair.generate_keypair().private_key
        with pytest.raises(SignerError, match="algorithm"):
            InProcessSigner(seed, algorithm="rsa-2048")

    def test_ecdsa_rejects_high_s_malleable_signature(self) -> None:
        """Low-S enforcement (F5): the n-s twin of a valid sig must be rejected."""
        from cryptography.hazmat.primitives.asymmetric.utils import (
            decode_dss_signature,
            encode_dss_signature,
        )

        # NIST P-256 group order n.
        n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
        seed = keypair.generate_keypair().private_key
        signer = InProcessSigner(seed, algorithm=ECDSA_P256)
        message = b"malleability-defense"
        r, s = decode_dss_signature(signer.sign(message))
        low_s = s if s <= n // 2 else n - s
        high_s = n - low_s
        low_sig = encode_dss_signature(r, low_s)
        high_sig = encode_dss_signature(r, high_s)
        # The canonical low-S signature verifies; its malleable high-S twin does not.
        assert verify_signature(ECDSA_P256, message, low_sig, signer.public_key)
        assert not verify_signature(ECDSA_P256, message, high_sig, signer.public_key)


# ---------------------------------------------------------------------------
# VaultSigner + VaultTransit — REQ-006 (seed NEVER materialises in-process)
# ---------------------------------------------------------------------------


class _SeedGuardTransit:
    """A fake transit that RAISES if anyone tries to obtain the raw seed.

    Signing is delegated to a locally-held key, but the seed is exposed ONLY
    through ``resolve_secret``/``seed``, both of which explode — proving the
    ``VaultSigner`` path never reaches for the seed (REQ-006).
    """

    def __init__(self, seed: bytes) -> None:
        self._kp = keypair.KeyPair.from_seed(seed)
        self.sign_calls: list[tuple[str, bytes]] = []

    # The out-of-process boundary the VaultSigner is allowed to use.
    def sign(self, key_ref: str, message: bytes) -> bytes:
        self.sign_calls.append((key_ref, message))
        return keypair.sign(message, self._kp.private_key)

    def public_key(self, key_ref: str) -> bytes:
        return self._kp.public_key

    # Anything that would hand the seed back to the agent must never be called.
    def resolve_secret(self, *_a: object, **_k: object) -> str:
        raise AssertionError("VaultSigner requested the raw seed (REQ-006 violation)")

    @property
    def seed(self) -> bytes:
        raise AssertionError("VaultSigner reached for the raw seed (REQ-006 violation)")


class TestVaultSigner:
    def test_signs_by_reference_seed_never_materialises(self) -> None:
        seed = keypair.generate_keypair().private_key
        transit = _SeedGuardTransit(seed)
        signer = VaultSigner(transit, key_ref="operator")
        message = b"sign-by-reference"
        signature = signer.sign(message)
        # Verifies with the public key the transit returned — no seed involved.
        assert verify_signature(ED25519, message, signature, signer.public_key)
        # The transit's sign() boundary was crossed; the seed accessors were not.
        assert transit.sign_calls == [("operator", message)]

    def test_public_key_sourced_from_transit(self) -> None:
        seed = keypair.generate_keypair().private_key
        transit = _SeedGuardTransit(seed)
        signer = VaultSigner(transit, key_ref="operator")
        assert signer.public_key == transit.public_key("operator")


# ---------------------------------------------------------------------------
# build_signer — REQ-007 (one config seam; fail-closed vault_transit)
# ---------------------------------------------------------------------------


class TestBuildSigner:
    def test_in_process_from_config(self) -> None:
        seed = keypair.generate_keypair().private_key
        cfg = SignerConfig(custody="in_process", algorithm="ed25519")
        signer = build_signer(cfg, seed=seed)
        assert isinstance(signer, InProcessSigner)
        assert signer.algorithm == ED25519

    def test_vault_transit_without_client_fails_closed(self) -> None:
        cfg = SignerConfig(custody="vault_transit", key_ref="operator")
        with pytest.raises(SignerError, match="vault_transit"):
            build_signer(cfg, seed=None, vault_transit=None)

    def test_vault_transit_never_falls_back_to_in_process_seed(self) -> None:
        """Even WITH a seed present, vault_transit + no client must fail closed."""
        seed = keypair.generate_keypair().private_key
        cfg = SignerConfig(custody="vault_transit", key_ref="operator")
        with pytest.raises(SignerError):
            build_signer(cfg, seed=seed, vault_transit=None)

    def test_vault_transit_with_client(self) -> None:
        seed = keypair.generate_keypair().private_key
        transit = _SeedGuardTransit(seed)
        cfg = SignerConfig(custody="vault_transit", key_ref="operator")
        signer = build_signer(cfg, vault_transit=transit)
        assert isinstance(signer, VaultSigner)

    def test_in_process_without_seed_fails_closed(self) -> None:
        cfg = SignerConfig(custody="in_process")
        with pytest.raises(SignerError, match="seed"):
            build_signer(cfg, seed=None)


# ---------------------------------------------------------------------------
# FileNotaryTransit — reference out-of-process signer (REQ-006, T-04)
# ---------------------------------------------------------------------------


class TestFileNotaryTransit:
    def test_round_trips_signature_without_exposing_seed(self, tmp_path: Path) -> None:
        seed = keypair.generate_keypair().private_key
        keystore = tmp_path / "notary"
        FileNotaryTransit.provision(keystore, "operator", seed)

        transit = FileNotaryTransit(keystore)
        message = b"out-of-process-attestation"
        signature = transit.sign("operator", message)
        public_key = transit.public_key("operator")

        # The signature the separate notary process produced verifies here,
        # where only the public key is known.
        assert verify_signature(ED25519, message, signature, public_key)

    def test_vault_signer_over_file_notary(self, tmp_path: Path) -> None:
        seed = keypair.generate_keypair().private_key
        keystore = tmp_path / "notary"
        FileNotaryTransit.provision(keystore, "operator", seed)
        signer = VaultSigner(FileNotaryTransit(keystore), key_ref="operator")
        message = b"end-to-end"
        signature = signer.sign(message)
        assert verify_signature(ED25519, message, signature, signer.public_key)

    def test_ecdsa_notary_round_trip(self, tmp_path: Path) -> None:
        """F1: the reference notary must serve ecdsa-p256 (federal out-of-process)."""
        seed = keypair.generate_keypair().private_key
        keystore = tmp_path / "notary"
        FileNotaryTransit.provision(keystore, "operator", seed, algorithm=ECDSA_P256)
        transit = FileNotaryTransit(keystore, algorithm=ECDSA_P256)
        signer = VaultSigner(transit, key_ref="operator", algorithm=ECDSA_P256)
        message = b"federal-out-of-process"
        signature = signer.sign(message)
        assert verify_signature(ECDSA_P256, message, signature, signer.public_key)
        # The public key the notary recorded is the ECDSA (DER) key, not Ed25519.
        assert signer.public_key != keypair.KeyPair.from_seed(seed).public_key
