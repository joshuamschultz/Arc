"""SPEC-037 T-14 — tier/config matrix (REQ-009): one seam, tier = stringency.

Asymmetric signing runs at every tier off the same ``build_signer`` seam;
``require_fips`` and ``vault_transit`` custody are federal floors toggled by
config (ADR-019), not separate code paths.
"""

from __future__ import annotations

import pytest

from arctrust import keypair
from arctrust.fips import ArcTrustFipsError, assert_fips_if_required
from arctrust.signer import (
    ECDSA_P256,
    ED25519,
    InProcessSigner,
    SignerConfig,
    VaultSigner,
    build_signer,
    verify_signature,
)


class _SeedGuardTransit:
    def __init__(self, seed: bytes) -> None:
        self._kp = keypair.KeyPair.from_seed(seed)

    def sign(self, key_ref: str, message: bytes) -> bytes:
        return keypair.sign(message, self._kp.private_key)

    def public_key(self, key_ref: str) -> bytes:
        return self._kp.public_key

    def resolve_secret(self, *_a: object, **_k: object) -> str:  # pragma: no cover
        raise AssertionError("seed requested under vault_transit (REQ-006)")


class TestPersonalTier:
    def test_personal_in_process_ed25519_end_to_end(self) -> None:
        """Personal (in_process, require_fips=False): Ed25519 signs unchanged."""
        seed = keypair.generate_keypair().private_key
        assert_fips_if_required(require_fips=False, algorithm=ED25519)  # no-op
        signer = build_signer(SignerConfig(custody="in_process", algorithm=ED25519), seed=seed)
        assert isinstance(signer, InProcessSigner)
        msg = b"personal-tier"
        assert verify_signature(ED25519, msg, signer.sign(msg), signer.public_key)


class TestFederalTier:
    def test_federal_fails_closed_on_non_fips_backend(self) -> None:
        """Federal (require_fips=True) fails closed on this non-FIPS dev backend."""
        with pytest.raises(ArcTrustFipsError):
            assert_fips_if_required(require_fips=True, algorithm=ECDSA_P256)

    def test_federal_vault_transit_signs_by_reference_no_seed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Federal + a validated backend: sign by reference; no seed in-process."""
        monkeypatch.setattr("arctrust.fips.fips_backend_active", lambda: True)
        # With FIPS satisfied and ECDSA-P256 selected, the floor passes.
        assert_fips_if_required(require_fips=True, algorithm=ECDSA_P256)

        seed = keypair.generate_keypair().private_key
        transit = _SeedGuardTransit(seed)
        signer = build_signer(
            SignerConfig(custody="vault_transit", algorithm=ED25519, key_ref="operator"),
            vault_transit=transit,
        )
        assert isinstance(signer, VaultSigner)
        msg = b"federal-by-reference"
        assert verify_signature(ED25519, msg, signer.sign(msg), signer.public_key)
