"""Trust store — Ed25519 public keys for operators and manifest issuers.

Federal-tier Arc deployments need to verify two classes of Ed25519 signatures
before accepting privileged actions:

1. **Operator pairing signatures** (``arcgateway.pairing``) — an operator
   approving a DM pairing code must sign the code+timestamp challenge with
   their Ed25519 private key.  The gateway verifies the signature against
   the operator's registered pubkey before consuming the pairing code.
2. **Backend manifest issuer signatures** (``arcrun.backends.loader``) — the
   signed ``allowed_backends`` manifest is signed by a trust authority.  The
   loader verifies the issuer signature before trusting the backend list.

Both use cases need the same primitive: "given a DID, return its public key".
This module provides that primitive against simple TOML files in ``~/.arc/trust/``:

``operators.toml``::

    [operators."did:arc:org:operator/abcd1234"]
    public_key = "BASE64_ENCODED_32_BYTE_ED25519_PUBKEY"
    added_at = "2026-04-18T00:00:00Z"
    notes = "Primary operator, Josh Schultz"

``issuers.toml``::

    [issuers."did:arc:org:trust-authority/abcd1234"]
    public_key = "BASE64_ENCODED_32_BYTE_ED25519_PUBKEY"
    added_at = "2026-04-18T00:00:00Z"
    role = "manifest-signer"

Security properties
-------------------
- Files must have 0600 permissions (owner read/write only).  Any group or
  other-readable mode raises ``TrustStoreError`` with code
  ``TRUST_STORE_INSECURE_PERMS`` — federal deployments cannot silently accept
  files that another user could have tampered with.
- Keys are base64-encoded 32-byte Ed25519 pubkeys (PyNaCl ``VerifyKey``).
  Malformed keys raise ``TrustStoreError`` with code ``TRUST_STORE_BAD_KEY``.
- A 60-second TTL cache reduces file-system churn when many signatures are
  verified in a short window.  Invalidation is explicit via
  ``invalidate_cache()`` — the cache never auto-reloads on file change, so a
  SIGHUP-driven reload can be implemented by the caller if needed.

Threat mitigations (OWASP for Agentic Applications):
- ASI03 (Identity & Privilege Abuse): no shared credentials; every approver
  has a distinct DID + key.
- ASI07 (Insecure Inter-Agent Communication): pairing/manifest signatures use
  Ed25519; this module is the authoritative pubkey source.
"""

from __future__ import annotations

import base64
import logging
import stat
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_logger = logging.getLogger("arctrust.trust_store")

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TrustStoreError(Exception):
    """Trust-store load, permission, or key-format failure.

    Carries a machine-readable ``code`` and optional ``details`` dict so
    callers can produce structured audit events without string parsing.
    """

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        return f"[{self.code}] trust_store: {self.message}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_DEFAULT_TRUST_DIR: Final[Path] = Path.home() / ".arc" / "trust"
_OPERATORS_FILE: Final[str] = "operators.toml"
_ISSUERS_FILE: Final[str] = "issuers.toml"
_CACHE_TTL_SECONDS: Final[int] = 60


@dataclass(frozen=True)
class _CacheEntry:
    """TTL cache entry for a loaded trust-store file."""

    records: dict[str, bytes]  # did → pubkey bytes
    loaded_at: float


_operator_cache: dict[Path, _CacheEntry] = {}
_issuer_cache: dict[Path, _CacheEntry] = {}


def load_operator_pubkey(
    did: str,
    *,
    trust_dir: Path | None = None,
) -> bytes:
    """Return the 32-byte Ed25519 pubkey for an operator DID.

    Reads ``~/.arc/trust/operators.toml`` (or ``trust_dir/operators.toml``
    when provided).  Enforces 0600 permissions on the file.

    Args:
        did:        Operator DID (exact match required).
        trust_dir:  Optional override directory.  Defaults to ``~/.arc/trust``.

    Returns:
        The 32-byte pubkey ready to pass to ``nacl.signing.VerifyKey``.

    Raises:
        TrustStoreError: File missing, insecure permissions, malformed key,
            or DID not registered.
    """
    records = _load_cached(
        directory=trust_dir or _DEFAULT_TRUST_DIR,
        filename=_OPERATORS_FILE,
        top_level_key="operators",
        cache=_operator_cache,
    )
    if did not in records:
        raise TrustStoreError(
            code="TRUST_STORE_DID_UNKNOWN",
            message=(
                f"Operator DID {did!r} is not registered in the trust store.  "
                "Add it to operators.toml with its base64 Ed25519 pubkey."
            ),
            details={"did": did, "file": _OPERATORS_FILE},
        )
    return records[did]


def load_issuer_pubkey(
    did: str,
    *,
    trust_dir: Path | None = None,
) -> bytes:
    """Return the 32-byte Ed25519 pubkey for a manifest-issuer DID.

    Reads ``~/.arc/trust/issuers.toml`` (or ``trust_dir/issuers.toml``).
    Enforces 0600 permissions on the file.

    Args:
        did:        Issuer DID.
        trust_dir:  Optional override directory.

    Returns:
        The 32-byte pubkey.

    Raises:
        TrustStoreError: File missing, insecure permissions, malformed key,
            or DID not registered.
    """
    records = _load_cached(
        directory=trust_dir or _DEFAULT_TRUST_DIR,
        filename=_ISSUERS_FILE,
        top_level_key="issuers",
        cache=_issuer_cache,
    )
    if did not in records:
        raise TrustStoreError(
            code="TRUST_STORE_DID_UNKNOWN",
            message=(
                f"Issuer DID {did!r} is not registered in the trust store.  "
                "Add it to issuers.toml with its base64 Ed25519 pubkey."
            ),
            details={"did": did, "file": _ISSUERS_FILE},
        )
    return records[did]


def invalidate_cache() -> None:
    """Flush operator and issuer pubkey caches.

    Intended for tests and for SIGHUP-driven reloads after manual edits
    to the trust files.
    """
    _operator_cache.clear()
    _issuer_cache.clear()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load_cached(
    *,
    directory: Path,
    filename: str,
    top_level_key: str,
    cache: dict[Path, _CacheEntry],
) -> dict[str, bytes]:
    """Return cached pubkey records for a trust-store file, loading if stale.

    Args:
        directory:      Trust directory (``~/.arc/trust`` or override).
        filename:       ``operators.toml`` or ``issuers.toml``.
        top_level_key:  ``"operators"`` or ``"issuers"``.
        cache:          Per-file TTL cache keyed by resolved path.

    Returns:
        Mapping of DID → Ed25519 pubkey bytes.
    """
    path = directory.expanduser().resolve() / filename
    now = time.monotonic()

    entry = cache.get(path)
    if entry is not None and now - entry.loaded_at < _CACHE_TTL_SECONDS:
        return entry.records

    records = _read_trust_file(path, top_level_key=top_level_key)
    cache[path] = _CacheEntry(records=records, loaded_at=now)
    return records


def _read_trust_file(path: Path, *, top_level_key: str) -> dict[str, bytes]:
    """Read a TOML trust file and return DID → pubkey bytes.

    Args:
        path:           Absolute path to the trust file.
        top_level_key:  The mandatory top-level TOML table.

    Raises:
        TrustStoreError: File missing, insecure permissions, malformed TOML,
            or malformed key.
    """
    if not path.exists():
        raise TrustStoreError(
            code="TRUST_STORE_FILE_MISSING",
            message=(
                f"Trust file {path} does not exist.  "
                "Create it with 0600 permissions and populate with DID → pubkey entries."
            ),
            details={"path": str(path)},
        )

    _enforce_0600_perms(path)

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TrustStoreError(
            code="TRUST_STORE_READ_FAILED",
            message=f"Cannot read trust file {path}: {exc}",
            details={"path": str(path)},
        ) from exc

    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise TrustStoreError(
            code="TRUST_STORE_BAD_TOML",
            message=f"Trust file {path} has invalid TOML syntax: {exc}",
            details={"path": str(path)},
        ) from exc

    section = data.get(top_level_key)
    if not isinstance(section, dict):
        raise TrustStoreError(
            code="TRUST_STORE_BAD_SCHEMA",
            message=(
                f"Trust file {path} is missing required top-level table "
                f"[{top_level_key}.*]."
            ),
            details={"path": str(path), "expected_section": top_level_key},
        )

    records: dict[str, bytes] = {}
    for did, entry in section.items():
        records[did] = _decode_pubkey(did=did, entry=entry, path=path)

    return records


def _decode_pubkey(*, did: str, entry: object, path: Path) -> bytes:
    """Extract and decode the base64 Ed25519 pubkey from a TOML entry.

    Raises:
        TrustStoreError: Entry shape wrong, key missing, or decode failure.
    """
    if not isinstance(entry, dict):
        raise TrustStoreError(
            code="TRUST_STORE_BAD_SCHEMA",
            message=(
                f"Trust file {path}: entry for DID {did!r} must be a TOML "
                "sub-table with a ``public_key`` field."
            ),
            details={"path": str(path), "did": did},
        )

    public_key_b64 = entry.get("public_key")
    if not isinstance(public_key_b64, str):
        raise TrustStoreError(
            code="TRUST_STORE_BAD_SCHEMA",
            message=(
                f"Trust file {path}: entry for DID {did!r} is missing a "
                "string ``public_key`` field."
            ),
            details={"path": str(path), "did": did},
        )

    try:
        raw = base64.b64decode(public_key_b64, validate=True)
    except ValueError as exc:
        raise TrustStoreError(
            code="TRUST_STORE_BAD_KEY",
            message=(
                f"Trust file {path}: public_key for DID {did!r} is not valid "
                f"base64 ({exc})."
            ),
            details={"path": str(path), "did": did},
        ) from exc

    if len(raw) != 32:
        raise TrustStoreError(
            code="TRUST_STORE_BAD_KEY",
            message=(
                f"Trust file {path}: public_key for DID {did!r} is "
                f"{len(raw)} bytes, expected 32 (Ed25519 pubkey)."
            ),
            details={"path": str(path), "did": did, "actual_length": len(raw)},
        )
    return raw


def _enforce_0600_perms(path: Path) -> None:
    """Raise TrustStoreError if the file is group- or other-readable/writable.

    Federal deployments require 0600 — any bit in the group or other triplets
    means a different user could have modified the file, which would let an
    attacker substitute their own pubkey for a trusted DID.
    """
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise TrustStoreError(
            code="TRUST_STORE_INSECURE_PERMS",
            message=(
                f"Trust file {path} has insecure permissions {oct(mode)}.  "
                "Expected 0600 (owner read/write only).  Run: chmod 0600 "
                f"{path}"
            ),
            details={"path": str(path), "permissions": oct(mode)},
        )


__all__ = [
    "TrustStoreError",
    "invalidate_cache",
    "load_issuer_pubkey",
    "load_operator_pubkey",
]
