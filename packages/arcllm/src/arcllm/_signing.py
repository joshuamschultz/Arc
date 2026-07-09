"""Request signing — asymmetric attestation via arctrust's ``Signer``.

Outbound-request attestation is NON-REPUDIABLE (AU-10): the signature verifies
with the public key alone, so a verifier never holds signing material. The
primitive lives in arctrust (Ed25519 default, ECDSA-P256 for the FIPS/federal
path); arcllm only decides *what* to sign (:func:`canonical_payload`) and
consumes the seam — it defines no signing primitive of its own (SPEC-037
REQ-001, boundary).
"""

from __future__ import annotations

import os
from typing import Any

from arctrust import canonical_json
from arctrust.signer import ECDSA_P256, ED25519, InProcessSigner, Signer

from arcllm.exceptions import ArcLLMConfigError
from arcllm.types import Message, Tool

_SEED_BYTES = 32


def canonical_payload(
    messages: list[Message],
    tools: list[Tool] | None,
    model: str,
) -> bytes:
    """Serialize request content to deterministic canonical JSON bytes."""
    data: dict[str, Any] = {
        "messages": [m.model_dump() for m in messages],
        "model": model,
        "tools": [t.model_dump() for t in tools] if tools else [],
    }
    return canonical_json(data)


def _decode_seed(value: str) -> bytes:
    """Decode a signing-key env value into a 32-byte Ed25519/ECDSA seed.

    The env var holds a hex-encoded 32-byte seed — the same shape a vault or
    KMS export uses. Symmetric secrets are no longer accepted.
    """
    try:
        seed = bytes.fromhex(value)
    except ValueError as e:
        raise ArcLLMConfigError(
            "signing key must be a hex-encoded 32-byte seed for asymmetric signing"
        ) from e
    if len(seed) != _SEED_BYTES:
        raise ArcLLMConfigError(
            f"signing key must decode to {_SEED_BYTES} bytes, got {len(seed)}"
        )
    return seed


def create_signer(algorithm: str, signing_key_env: str) -> Signer:
    """Factory: build an arctrust :class:`Signer` from algorithm + env seed.

    Raises:
        ArcLLMConfigError: On missing env var or an unsupported (e.g. symmetric)
            algorithm. Only asymmetric ``ed25519`` / ``ecdsa-p256`` are valid.
    """
    key_value = os.environ.get(signing_key_env)
    if key_value is None:
        raise ArcLLMConfigError(f"Signing key environment variable '{signing_key_env}' not set")

    if algorithm not in (ED25519, ECDSA_P256):
        raise ArcLLMConfigError(
            f"Unsupported signing algorithm: '{algorithm}'. "
            f"Supported: '{ED25519}', '{ECDSA_P256}' (asymmetric only — HMAC is removed)"
        )

    return InProcessSigner(_decode_seed(key_value), algorithm)
