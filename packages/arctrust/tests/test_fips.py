"""SPEC-037 — the generalised arctrust FIPS gate (REQ-008, REQ-009).

One gate covers signing AND encryption: at federal / ``require_fips=true`` it
fails closed unless the loaded crypto backend is FIPS-validated AND the chosen
algorithm is FIPS-approved for it. Ed25519 (PyNaCl/libsodium, non-CMVP) is
rejected under FIPS, forcing ECDSA-P256; personal/enterprise run unchanged.
Tier is stringency, not a feature gate (ADR-019).
"""

from __future__ import annotations

import pytest

from arctrust.fips import (
    ArcTrustFipsError,
    algorithm_is_fips_approved,
    assert_fips_if_required,
    fips_backend_active,
)


class TestAlgorithmApproval:
    def test_ecdsa_p256_is_fips_approved(self) -> None:
        assert algorithm_is_fips_approved("ecdsa-p256")

    def test_aes_256_gcm_is_fips_approved(self) -> None:
        assert algorithm_is_fips_approved("aes-256-gcm")

    def test_ed25519_not_fips_approved_here(self) -> None:
        # Arc's Ed25519 is PyNaCl/libsodium, which has no CMVP validation —
        # so it can never satisfy the federal floor; the gate must force ECDSA.
        assert not algorithm_is_fips_approved("ed25519")


class TestAssertGate:
    def test_non_fips_passes_when_not_required(self) -> None:
        # Personal/enterprise: require_fips=false runs Ed25519/PyNaCl unchanged.
        assert_fips_if_required(require_fips=False, algorithm="ed25519")

    def test_required_but_backend_not_validated_fails_closed(self) -> None:
        # The PyPI-wheel OpenSSL is never CMVP-validated, so this env's backend
        # is non-FIPS — federal must refuse to proceed (SC-13 fail-closed).
        assert not fips_backend_active()  # precondition for this env
        with pytest.raises(ArcTrustFipsError):
            assert_fips_if_required(require_fips=True, algorithm="ecdsa-p256")

    def test_required_rejects_non_approved_algorithm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With a (simulated) validated backend, Ed25519 is still rejected —
        # the algorithm floor forces ECDSA-P256 at federal.
        monkeypatch.setattr("arctrust.fips.fips_backend_active", lambda: True)
        with pytest.raises(ArcTrustFipsError, match="algorithm"):
            assert_fips_if_required(require_fips=True, algorithm="ed25519")

    def test_required_with_validated_backend_and_approved_algorithm_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("arctrust.fips.fips_backend_active", lambda: True)
        assert_fips_if_required(require_fips=True, algorithm="ecdsa-p256")
