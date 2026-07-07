"""The generalised FIPS gate — one startup floor for signing AND encryption.

Federal deployments (SC-13 Cryptographic Protection, IA-7 Crypto Module
Authentication) must use FIPS 140-3 validated crypto for any protected
function. This gate is the single fail-closed self-check: at
``require_fips=true`` it refuses to proceed unless BOTH the loaded crypto
backend is FIPS-validated AND the selected algorithm is FIPS-approved for it.

It supersedes the encryption-only gate that used to live in
``arcllm._trace_crypto`` — that copy is deleted and arcllm imports this one, so
there is a single source of truth for the FIPS posture.

Algorithm floor
---------------
- ``ecdsa-p256`` and ``aes-256-gcm`` are FIPS-approved (present in every CMVP
  OpenSSL/HSM).
- ``ed25519`` is NOT accepted here: Arc's Ed25519 is PyNaCl/libsodium, which
  has no CMVP validation path — so at federal the gate forces ECDSA-P256.
  (EdDSA is approved in FIPS 186-5, but only a later-3.x validated OpenSSL
  build exposes it; a deployment on such a module supplies its own adapter.)

Tier is stringency metadata (ADR-019): the same primitives run at every tier;
``require_fips`` (a config floor, true at federal) is what arms the gate.
There is no public ``cryptography`` API for the backend FIPS flag
(pyca/cryptography #7722), so :func:`fips_backend_active` reads the internal
``backend._fips_enabled`` — the value reflects whichever OpenSSL build is
actually linked (a system CMVP module in federal deployments).
"""

from __future__ import annotations

_FIPS_APPROVED_ALGORITHMS = frozenset({"ecdsa-p256", "aes-256-gcm"})


class ArcTrustFipsError(RuntimeError):
    """The federal FIPS floor was not met — refuse to proceed (fail-closed).

    Raised at startup when ``require_fips`` is set but the loaded crypto
    backend is not FIPS-validated, or the selected algorithm is not
    FIPS-approved (SC-13 / IA-7).
    """


def fips_backend_active() -> bool:
    """Return True when the loaded PyCA OpenSSL provider is FIPS-140-3-approved.

    Reads the internal ``backend._fips_enabled`` flag (no public API exists).
    The PyPI-wheel OpenSSL is never CMVP-validated; a federal deployment links
    a system OpenSSL FIPS provider, which flips this flag.
    """
    try:
        from cryptography.hazmat.backends.openssl.backend import backend as _ossl_backend
    except ImportError:
        return False
    return bool(getattr(_ossl_backend, "_fips_enabled", False))


def algorithm_is_fips_approved(algorithm: str) -> bool:
    """Return True when ``algorithm`` is FIPS-approved for Arc's backends."""
    return algorithm in _FIPS_APPROVED_ALGORITHMS


def assert_fips_if_required(*, require_fips: bool, algorithm: str) -> None:
    """Federal startup floor (SC-13 / IA-7). Fail-closed.

    Args:
        require_fips: When True (federal tier), enforce the floor. Personal /
            enterprise leave this False and this is a no-op.
        algorithm: The crypto algorithm the protected function will use
            (``ecdsa-p256``, ``aes-256-gcm``, ``ed25519``, ...).

    Raises:
        ArcTrustFipsError: When ``require_fips`` is set and either the backend
            is not FIPS-validated or the algorithm is not FIPS-approved.
    """
    if not require_fips:
        return
    if not fips_backend_active():
        raise ArcTrustFipsError(
            "require_fips=true but the loaded cryptography/OpenSSL provider is "
            "not FIPS-140-3-validated — refusing to run a protected crypto "
            "function under a non-validated module (SC-13 fail-closed). Supply "
            "a CMVP-validated OpenSSL FIPS provider (see SPEC-037 SDD §6.4)."
        )
    if not algorithm_is_fips_approved(algorithm):
        raise ArcTrustFipsError(
            f"require_fips=true but algorithm {algorithm!r} is not FIPS-approved "
            "for the loaded backend — select 'ecdsa-p256' for signing at federal "
            "(Arc's Ed25519 is PyNaCl/libsodium, which has no CMVP validation)."
        )


__all__ = [
    "ArcTrustFipsError",
    "algorithm_is_fips_approved",
    "assert_fips_if_required",
    "fips_backend_active",
]
