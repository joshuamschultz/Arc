"""Encrypt/decrypt by reference: the custody key stays in the transit, never here.

:class:`TransitCipher` is the seam a ``vault_transit`` deployment's transit
implements alongside signing (:class:`arctrust.signer.VaultTransit`): the caller
hands plaintext plus associated data to a named key and gets back an opaque
ciphertext string, and the reverse. The caller never holds the key.

:func:`notary_encrypt` / :func:`notary_decrypt` are the reference implementation
behind :class:`arctrust.signer.FileNotaryTransit`: each call spawns the
``_notary_cipher.py`` child, the only code that reads the key file.
AES-256-GCM (FIPS-approved) with a random 96-bit nonce per call; the key file
is ``<keystore>/<key_ref>.aes256`` (``0600``, minted by the child on first
encrypt, never on decrypt). A HashiCorp Vault / PKCS#11 adapter implements the
same two methods against its own non-exportable key.

Two failures are kept apart, because they need different operator actions:
:class:`TransitDecryptError` (the ciphertext does not open for this key and
associated data: tampered, moved, or another key) and
:class:`TransitUnavailableError` (the transit cannot answer at all: keystore
missing, key file unsafe, child failed or timed out). Neither carries material.
"""

from __future__ import annotations

import base64
import binascii
import re
import subprocess
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

#: Ciphertext envelope the reference notary returns.
NOTARY_PREFIX = "notary:v1:"
#: Child exit codes (see :mod:`arctrust._notary_cipher`).
EXIT_DECRYPT_FAILED = 3
_KEY_SUFFIX = ".aes256"
_KEY_REF = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_NONCE_BYTES = 12
_TAG_BYTES = 16
_MAX_AAD = 1024
_TIMEOUT_S = 10
_CHILD = Path(__file__).resolve().parent / "_notary_cipher.py"


class TransitDecryptError(ValueError):
    """The ciphertext does not open under this key with this associated data."""


class TransitUnavailableError(RuntimeError):
    """The transit could not perform the operation (fail closed, never fall back)."""


@runtime_checkable
class TransitCipher(Protocol):
    """Authenticated encryption under a key the caller never holds."""

    def encrypt(self, key_ref: str, plaintext: bytes, *, aad: bytes) -> str: ...

    def decrypt(self, key_ref: str, ciphertext: str, *, aad: bytes) -> bytes: ...


def _key_path(keystore: Path, key_ref: str) -> Path:
    if _KEY_REF.fullmatch(key_ref) is None:
        raise TransitUnavailableError("invalid transit key reference")
    return keystore / f"{key_ref}{_KEY_SUFFIX}"


def _frame(aad: bytes, payload: bytes) -> bytes:
    if len(aad) > _MAX_AAD:
        raise TransitUnavailableError("associated data exceeds limit")
    return len(aad).to_bytes(4, "big") + aad + payload


def _run_child(key_path: Path, action: str, framed: bytes) -> subprocess.CompletedProcess[bytes]:
    # Run the child as a standalone script in isolated mode (-I): it imports only
    # the stdlib and ``cryptography``, so it starts in milliseconds instead of
    # paying for the whole arctrust package, and ignores PYTHON* env overrides.
    try:
        return subprocess.run(  # noqa: S603 — fixed argv, no shell, no untrusted input
            [sys.executable, "-I", str(_CHILD), str(key_path), action],
            input=framed,
            capture_output=True,
            check=False,
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        raise TransitUnavailableError("transit notary did not answer") from None


def notary_encrypt(keystore: Path, key_ref: str, plaintext: bytes, *, aad: bytes) -> str:
    """Encrypt in the notary child; return ``notary:v1:<b64(nonce||ct||tag)>``."""
    result = _run_child(_key_path(keystore, key_ref), "encrypt", _frame(aad, plaintext))
    if result.returncode != 0 or len(result.stdout) < _NONCE_BYTES + _TAG_BYTES:
        raise TransitUnavailableError("transit notary refused to encrypt")
    return NOTARY_PREFIX + base64.b64encode(result.stdout).decode("ascii")


def notary_decrypt(keystore: Path, key_ref: str, ciphertext: str, *, aad: bytes) -> bytes:
    """Decrypt in the notary child, refusing anything not bound to ``aad``."""
    key_path = _key_path(keystore, key_ref)
    if not ciphertext.startswith(NOTARY_PREFIX):
        raise TransitDecryptError("ciphertext was not made by this transit")
    try:
        raw = base64.b64decode(ciphertext[len(NOTARY_PREFIX) :], validate=True)
    except (binascii.Error, ValueError):
        raise TransitDecryptError("ciphertext is not well-formed") from None
    if len(raw) < _NONCE_BYTES + _TAG_BYTES:
        raise TransitDecryptError("ciphertext is not well-formed")
    result = _run_child(key_path, "decrypt", _frame(aad, raw))
    if result.returncode == EXIT_DECRYPT_FAILED:
        raise TransitDecryptError("ciphertext does not open under this key")
    if result.returncode != 0:
        raise TransitUnavailableError("transit notary refused to decrypt")
    return result.stdout


__all__ = [
    "NOTARY_PREFIX",
    "TransitCipher",
    "TransitDecryptError",
    "TransitUnavailableError",
    "notary_decrypt",
    "notary_encrypt",
]
