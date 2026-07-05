"""Trace envelope encryption — AES-256-GCM content, AES Key Wrap for the DEK.

Federal-tier bodies are sealed before they ever touch disk (SPEC-016 D-438).
A fresh 256-bit data key (DEK) is generated per record and used exactly once
— nonce reuse on the content key is structurally impossible because each
DEK only ever encrypts one plaintext. The DEK is itself wrapped under a
single, vault-resolved wrapping key (KEK) using AES Key Wrap (RFC 3394 /
NIST SP 800-38F) — a nonce-free primitive purpose-built for wrapping keys,
not a second GCM call. The GCM additional-authenticated-data (AAD) binds
the ciphertext to ``trace_id``+``timestamp`` so a valid ciphertext cannot
be transplanted onto a different record (D-448).

``cryptography`` is imported lazily inside each function — importing this
module costs nothing when encryption is disabled (NFR-4); the encryption-
off path never touches the optional ``arcllm[trace-encryption]`` extra.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import jcs  # type: ignore[import-untyped]  # RFC 8785 canonical JSON — no stubs available

from arcllm.exceptions import ArcLLMConfigError, ArcLLMTraceIntegrityError
from arcllm.trace_store import EncryptedEnvelope

# 256-bit data key, 96-bit GCM nonce (NIST SP 800-38D recommended size).
_DEK_BITS = 256
_GCM_NONCE_BYTES = 12

_MISSING_EXTRA_MSG = (
    "encryption enabled but arcllm[trace-encryption] not installed "
    "(pip install arcllm[trace-encryption])"
)


def _import_crypto_primitives() -> tuple[Any, Any, Any]:
    """Lazily import the three crypto primitives this module needs.

    Raises:
        ArcLLMConfigError: When ``arcllm[trace-encryption]`` is not
            installed (fail-closed — never falls back to plaintext).
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.keywrap import aes_key_unwrap, aes_key_wrap
    except ImportError as e:
        raise ArcLLMConfigError(_MISSING_EXTRA_MSG) from e
    return AESGCM, aes_key_wrap, aes_key_unwrap


def fips_provider_active() -> bool:
    """Return True when the loaded OpenSSL provider is FIPS-140-3-approved.

    There is no public ``cryptography`` API for this (pyca/cryptography
    #7722) — the vendored PyPI-wheel OpenSSL is never CMVP-validated. This
    reads the internal backend flag, which reflects the FIPS status of
    whichever OpenSSL build is actually loaded (a system OpenSSL swapped
    in via ``CRYPTOGRAPHY_OPENSSL_NO_LEGACY``/build config is what
    federal deployments must supply, per SDD Research Insights SC-13).
    """
    try:
        from cryptography.hazmat.backends.openssl.backend import backend as _ossl_backend
    except ImportError:
        return False
    return bool(getattr(_ossl_backend, "_fips_enabled", False))


def assert_fips_provider_if_required(*, require_fips: bool) -> None:
    """Fail-closed startup self-check for the federal tier (SC-13, D-438).

    Args:
        require_fips: When True, refuse to proceed unless the loaded
            crypto provider is FIPS-140-3-approved. Personal/enterprise
            tiers leave this False.

    Raises:
        ArcLLMConfigError: When ``require_fips`` is True and the provider
            is not FIPS-approved.
    """
    if require_fips and not fips_provider_active():
        raise ArcLLMConfigError(
            "encryption.require_fips=true but the loaded cryptography/OpenSSL "
            "provider is not FIPS-140-3-approved — refusing to seal trace "
            "bodies under a non-validated crypto module (SC-13 fail-closed)"
        )


def decode_wrapping_key(secret: str) -> bytes:
    """Decode a resolved vault/env secret into 32 raw wrapping-key bytes.

    The secret is expected to be base64-encoded 256-bit key material (the
    standard shape for a vault-provisioned or KMS-exported wrapping key).

    Raises:
        ArcLLMConfigError: If the secret is not valid base64 or does not
            decode to exactly 32 bytes.
    """
    try:
        key_bytes = base64.b64decode(secret, validate=True)
    except (ValueError, TypeError) as e:
        raise ArcLLMConfigError(
            "trace encryption wrapping key must be base64-encoded 256-bit key material"
        ) from e
    if len(key_bytes) != 32:
        raise ArcLLMConfigError(
            f"trace encryption wrapping key must decode to 32 bytes, got {len(key_bytes)}"
        )
    return key_bytes


def _build_aad(trace_id: str, timestamp: str) -> str:
    """Canonical AAD string binding ciphertext to record identity (D-448)."""
    return f"{trace_id}:{timestamp}"


def seal(
    bodies: dict[str, Any],
    *,
    trace_id: str,
    timestamp: str,
    wrapping_key: bytes,
    key_ref: str,
) -> EncryptedEnvelope:
    """Seal ``bodies`` into an :class:`EncryptedEnvelope`.

    A fresh 256-bit data key (DEK) encrypts ``bodies`` under AES-256-GCM
    with a random 96-bit nonce; the DEK is then wrapped under
    ``wrapping_key`` (the resolved KEK) with AES Key Wrap. ``key_ref``
    rides the envelope so a future KEK rotation never needs to rewrite
    the log — old records unseal via the ``key_ref`` that wrapped them.

    Raises:
        ArcLLMConfigError: When ``arcllm[trace-encryption]`` is missing.
    """
    aesgcm_cls, aes_key_wrap, _ = _import_crypto_primitives()

    data_key = aesgcm_cls.generate_key(bit_length=_DEK_BITS)
    nonce = os.urandom(_GCM_NONCE_BYTES)
    aad = _build_aad(trace_id, timestamp)
    plaintext = jcs.canonicalize(bodies)
    ciphertext = aesgcm_cls(data_key).encrypt(nonce, plaintext, aad.encode("utf-8"))
    wrapped_key = aes_key_wrap(wrapping_key, data_key)

    return EncryptedEnvelope(
        wrapped_key=base64.b64encode(wrapped_key).decode("ascii"),
        key_ref=key_ref,
        nonce=base64.b64encode(nonce).decode("ascii"),
        ciphertext=base64.b64encode(ciphertext).decode("ascii"),
        aad=aad,
    )


def unseal(
    envelope: EncryptedEnvelope,
    *,
    trace_id: str,
    timestamp: str,
    wrapping_key: bytes,
) -> dict[str, Any]:
    """Unseal an :class:`EncryptedEnvelope` back into its plaintext bodies.

    Args:
        envelope: The sealed envelope read from a ``TraceRecord``.
        trace_id: The record's OWN ``trace_id`` (from the record being
            read, not attacker-suppliable) — must match the AAD bound at
            seal time.
        timestamp: The record's own ``timestamp``, same binding as above.
        wrapping_key: The KEK that wrapped this envelope's DEK (resolved
            by the caller via ``envelope.key_ref``).

    Raises:
        ArcLLMTraceIntegrityError: When the record's own trace_id/timestamp
            no longer match the AAD bound at seal time (D-448 anti-
            transplant) — this is a tamper signal, distinct from a config
            problem.
        ArcLLMConfigError: When ``arcllm[trace-encryption]`` is missing, or
            when the AEAD tag fails to authenticate (wrong key / corrupted
            ciphertext).
    """
    expected_aad = _build_aad(trace_id, timestamp)
    if envelope.aad != expected_aad:
        raise ArcLLMTraceIntegrityError(
            f"trace envelope AAD mismatch: expected '{expected_aad}', "
            f"got '{envelope.aad}' — possible ciphertext transplant (D-448)"
        )

    aesgcm_cls, _, aes_key_unwrap = _import_crypto_primitives()

    nonce = base64.b64decode(envelope.nonce)
    ciphertext = base64.b64decode(envelope.ciphertext)

    # Both the key-unwrap step and the GCM tag check can fail on a wrong
    # wrapping key (unwrap fails first when the wrapped integrity check
    # inside AES Key Wrap itself rejects it; GCM catches anything that
    # slips past that). Either failure means the same thing to a caller:
    # this envelope cannot be trusted with the given key.
    try:
        data_key = aes_key_unwrap(wrapping_key, base64.b64decode(envelope.wrapped_key))
        plaintext = aesgcm_cls(data_key).decrypt(nonce, ciphertext, expected_aad.encode("utf-8"))
    except Exception as e:  # reason: cryptography raises its own InvalidUnwrap/InvalidTag types
        raise ArcLLMConfigError(
            "trace envelope failed to authenticate — wrong wrapping key or corrupted ciphertext"
        ) from e

    result: dict[str, Any] = json.loads(plaintext)
    return result


__all__ = [
    "assert_fips_provider_if_required",
    "decode_wrapping_key",
    "fips_provider_active",
    "seal",
    "unseal",
]
