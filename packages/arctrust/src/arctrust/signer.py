"""The ``Signer`` seam — asymmetric signing with pluggable key custody.

Every non-repudiable signature Arc emits (WORM audit chains, operator
attestation, arcllm request signing, arcteam audit chains) resolves through a
:class:`Signer`. Two custody models sit behind the one Protocol:

- :class:`InProcessSigner` holds the private seed in memory. Ed25519 (via
  :mod:`arctrust.keypair` / PyNaCl) is the personal/enterprise default;
  ECDSA-P256 (via PyCA ``cryptography`` against the linked OpenSSL) is the
  FIPS/federal option.
- :class:`VaultSigner` signs **by reference**: it hands the message to a
  :class:`VaultTransit` boundary (Vault Transit, a PKCS#11 HSM, cloud KMS, or
  the reference :class:`FileNotaryTransit`) and receives a signature. The seed
  NEVER enters this process — closing the SPEC-053 in-process-seed residual
  (REQ-006).

Tier is stringency metadata (ADR-019): the same seam runs at every tier;
``custody`` and ``algorithm`` are config-selected. ``build_signer`` fails
closed — ``vault_transit`` with no transit client is an error, never a silent
in-process fallback (NFR-3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from arctrust import keypair

ED25519 = "ed25519"
ECDSA_P256 = "ecdsa-p256"
_SUPPORTED_ALGORITHMS = frozenset({ED25519, ECDSA_P256})

IN_PROCESS = "in_process"
VAULT_TRANSIT = "vault_transit"

# NIST P-256 (secp256r1) group order n. A 32-byte seed is reduced into
# [1, n-1] to derive a deterministic private scalar (RFC 6979 domain, no public
# constant is exported by ``cryptography``).
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


class SignerError(RuntimeError):
    """A signer could not be constructed or a custody invariant was violated.

    Raised fail-closed — e.g. an unsupported algorithm, a missing seed under
    in-process custody, or a missing transit client under vault-transit custody
    (never a silent downgrade to in-process signing).
    """


@runtime_checkable
class Signer(Protocol):
    """A source of non-repudiable signatures over arbitrary bytes.

    The verifier only ever needs :attr:`public_key` and :attr:`algorithm`; the
    private material lives behind :meth:`sign` (in-process or out-of-process).
    """

    @property
    def public_key(self) -> bytes: ...

    @property
    def algorithm(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


@runtime_checkable
class VaultTransit(Protocol):
    """The out-of-process signing boundary (sign-by-reference).

    A concrete transit (Vault Transit, PKCS#11 HSM, cloud KMS, or the reference
    :class:`FileNotaryTransit`) holds the seed; the caller only sends a message
    and receives a signature. Deliberately exposes NO seed accessor.
    """

    def sign(self, key_ref: str, message: bytes) -> bytes: ...

    def public_key(self, key_ref: str) -> bytes: ...


# ---------------------------------------------------------------------------
# ECDSA-P256 primitives (PyCA cryptography — the FIPS-approvable path)
# ---------------------------------------------------------------------------


def _ecdsa_private_key(seed: bytes):  # type: ignore[no-untyped-def]  # cryptography objects are untyped
    """Derive a deterministic P-256 private key from a 32-byte seed."""
    from cryptography.hazmat.primitives.asymmetric import ec

    if len(seed) != keypair.KEY_SIZE:
        raise SignerError(f"ecdsa-p256 seed must be {keypair.KEY_SIZE} bytes, got {len(seed)}")
    scalar = int.from_bytes(seed, "big") % (_P256_ORDER - 1) + 1
    return ec.derive_private_key(scalar, ec.SECP256R1())


def _ecdsa_public_bytes(seed: bytes) -> bytes:
    from cryptography.hazmat.primitives import serialization

    public = _ecdsa_private_key(seed).public_key()
    der: bytes = public.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return der


def _ecdsa_sign(seed: bytes, message: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature,
        encode_dss_signature,
    )

    raw: bytes = _ecdsa_private_key(seed).sign(message, ec.ECDSA(hashes.SHA256()))
    # Emit the canonical low-S form so every Arc ECDSA signature is non-malleable
    # and passes the low-S check in ``_ecdsa_verify`` (F5). PyCA's signer picks a
    # random k and may return either the low- or high-S encoding.
    r, s = decode_dss_signature(raw)
    if s > _P256_ORDER // 2:
        s = _P256_ORDER - s
    return encode_dss_signature(r, s)


def _ecdsa_verify(message: bytes, signature: bytes, public_key: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    try:
        # Reject high-S (malleable) signatures: only the canonical s <= n/2 form
        # is accepted so an adversary cannot forge a second valid encoding
        # (n - s) of an existing signature (defense-in-depth, F5).
        _, s = decode_dss_signature(signature)
        if s > _P256_ORDER // 2:
            return False
        loaded = serialization.load_der_public_key(public_key)
        if not isinstance(loaded, ec.EllipticCurvePublicKey):
            return False
        loaded.verify(signature, message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def verify_signature(algorithm: str, message: bytes, signature: bytes, public_key: bytes) -> bool:
    """Algorithm-dispatched verification. Never raises — returns False on any error.

    The single verification entry point shared by :func:`arctrust.audit.verify_chain`
    and the arcteam audit chain, so a record's stored ``algorithm`` drives the
    check without either caller re-implementing the primitive.
    """
    if algorithm == ED25519:
        return keypair.verify(message, signature, public_key)
    if algorithm == ECDSA_P256:
        return _ecdsa_verify(message, signature, public_key)
    return False


# ---------------------------------------------------------------------------
# InProcessSigner — holds the seed (personal / enterprise / dev)
# ---------------------------------------------------------------------------


class InProcessSigner:
    """Signs in-process with a seed held in memory (Ed25519 or ECDSA-P256)."""

    def __init__(self, seed: bytes, algorithm: str = ED25519) -> None:
        if algorithm not in _SUPPORTED_ALGORITHMS:
            raise SignerError(
                f"unsupported signing algorithm {algorithm!r}; "
                f"supported: {sorted(_SUPPORTED_ALGORITHMS)}"
            )
        self._algorithm = algorithm
        self._seed = seed
        if algorithm == ED25519:
            self._public_key = keypair.KeyPair.from_seed(seed).public_key
        else:
            self._public_key = _ecdsa_public_bytes(seed)

    @property
    def public_key(self) -> bytes:
        return self._public_key

    @property
    def algorithm(self) -> str:
        return self._algorithm

    def sign(self, message: bytes) -> bytes:
        if self._algorithm == ED25519:
            return keypair.sign(message, self._seed)
        return _ecdsa_sign(self._seed, message)


# ---------------------------------------------------------------------------
# VaultSigner — signs by reference (enterprise / federal, out-of-process)
# ---------------------------------------------------------------------------


class VaultSigner:
    """Signs by reference through a :class:`VaultTransit`; the seed never enters
    this process (REQ-006). The public key is cached from the transit."""

    def __init__(self, transit: VaultTransit, key_ref: str, algorithm: str = ED25519) -> None:
        if algorithm not in _SUPPORTED_ALGORITHMS:
            raise SignerError(
                f"unsupported signing algorithm {algorithm!r}; "
                f"supported: {sorted(_SUPPORTED_ALGORITHMS)}"
            )
        self._transit = transit
        self._key_ref = key_ref
        self._algorithm = algorithm
        self._public_key = transit.public_key(key_ref)

    @property
    def public_key(self) -> bytes:
        return self._public_key

    @property
    def algorithm(self) -> str:
        return self._algorithm

    def sign(self, message: bytes) -> bytes:
        return self._transit.sign(self._key_ref, message)


# ---------------------------------------------------------------------------
# FileNotaryTransit — reference out-of-process signer for dev / CI
# ---------------------------------------------------------------------------


class FileNotaryTransit:
    """Reference :class:`VaultTransit`: signs via a separate notary process.

    A genuine out-of-process signer for dev/CI without a production HSM. The
    seed lives in the notary keystore (``<key_ref>.seed``, ``0600``); each
    :meth:`sign` spawns ``python -m arctrust._notary`` which is the only code
    that reads the seed. The concrete HashiCorp Vault / PKCS#11 binding is a
    deployment adapter that implements the same :class:`VaultTransit` Protocol.
    """

    _SEED_SUFFIX = ".seed"
    _PUB_SUFFIX = ".pub"

    def __init__(self, keystore: Path, algorithm: str = ED25519) -> None:
        if algorithm not in _SUPPORTED_ALGORITHMS:
            raise SignerError(
                f"unsupported signing algorithm {algorithm!r}; "
                f"supported: {sorted(_SUPPORTED_ALGORITHMS)}"
            )
        self._keystore = Path(keystore)
        self._algorithm = algorithm

    @classmethod
    def provision(
        cls, keystore: Path, key_ref: str, seed: bytes, algorithm: str = ED25519
    ) -> None:
        """Write the notary key material (seed ``0600`` + public key sentinel).

        The recorded public key matches ``algorithm`` so a federal
        (``ecdsa-p256``) notary hands back the ECDSA verify key, not an Ed25519
        one (F1).
        """
        keystore = Path(keystore)
        keystore.mkdir(parents=True, exist_ok=True)
        seed_path = keystore / f"{key_ref}{cls._SEED_SUFFIX}"
        seed_path.write_bytes(seed)
        seed_path.chmod(0o600)
        if algorithm == ED25519:
            public_key = keypair.KeyPair.from_seed(seed).public_key
        else:
            public_key = _ecdsa_public_bytes(seed)
        (keystore / f"{key_ref}{cls._PUB_SUFFIX}").write_bytes(public_key)

    def sign(self, key_ref: str, message: bytes) -> bytes:
        import os
        import subprocess
        import sys

        seed_path = self._keystore / f"{key_ref}{self._SEED_SUFFIX}"
        # The notary subprocess must import arctrust; point it at this src root.
        src_root = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{src_root}{os.pathsep}{existing}" if existing else str(src_root)

        # control) — no shell, no untrusted input. This IS the out-of-process
        # boundary that keeps the seed out of the caller's memory (REQ-006). The
        # algorithm is passed so the notary child signs with the configured
        # primitive (Ed25519 or ECDSA-P256, F1).
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "arctrust._notary", str(seed_path), self._algorithm],
            input=message,
            capture_output=True,
            check=True,
            env=env,
        )
        return result.stdout

    def public_key(self, key_ref: str) -> bytes:
        return (self._keystore / f"{key_ref}{self._PUB_SUFFIX}").read_bytes()


# ---------------------------------------------------------------------------
# build_signer — the config-selected seam (REQ-007, fail-closed NFR-3)
# ---------------------------------------------------------------------------


class SignerConfig(BaseModel):
    """Config that selects a signer: custody model + algorithm + key reference."""

    custody: str = Field(default=IN_PROCESS, description="in_process | vault_transit")
    algorithm: str = Field(default=ED25519, description="ed25519 | ecdsa-p256")
    key_ref: str = Field(default="", description="vault_transit key reference")


def build_signer(
    config: SignerConfig,
    *,
    seed: bytes | None = None,
    vault_transit: VaultTransit | None = None,
) -> Signer:
    """Resolve a :class:`Signer` from config.

    - ``custody=in_process`` → :class:`InProcessSigner` (requires ``seed``).
    - ``custody=vault_transit`` → :class:`VaultSigner` (requires
      ``vault_transit``). A missing transit client is a hard error, NEVER a
      silent in-process fallback even if a seed is present (NFR-3).
    """
    if config.custody == IN_PROCESS:
        if seed is None:
            raise SignerError("in_process custody requires a seed")
        return InProcessSigner(seed, config.algorithm)
    if config.custody == VAULT_TRANSIT:
        if vault_transit is None:
            raise SignerError(
                "vault_transit custody requires a transit client — refusing to "
                "fall back to in-process signing (fail-closed, NFR-3)"
            )
        return VaultSigner(vault_transit, config.key_ref, config.algorithm)
    raise SignerError(
        f"unknown custody model {config.custody!r}; expected in_process | vault_transit"
    )


__all__ = [
    "ECDSA_P256",
    "ED25519",
    "IN_PROCESS",
    "VAULT_TRANSIT",
    "FileNotaryTransit",
    "InProcessSigner",
    "Signer",
    "SignerConfig",
    "SignerError",
    "VaultSigner",
    "VaultTransit",
    "build_signer",
    "verify_signature",
]
