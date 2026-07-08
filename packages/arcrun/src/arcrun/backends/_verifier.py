"""Manifest signature + content-hash verification for backend load.

Implements the Ed25519 signature check on the ``allowed_backends`` TOML
manifest plus the per-backend SHA-256 content-hash check that catches a
tampered or swapped wheel. Both helpers raise
:class:`BackendSignatureError` on any failure and emit AuditEvents via
the helpers in ``_audit``.

Public symbols are re-exported from ``arcrun.backends.loader`` so test
code can keep the existing import path.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import tomllib
from pathlib import Path
from typing import Any

from arcrun.backends._audit import (
    emit_content_mismatch,
    emit_sig_invalid,
    emit_sig_verified,
)


class BackendSignatureError(RuntimeError):
    """Raised when a backend is not in the allowed_backends manifest,
    or when the manifest signature / content hashes fail verification,
    or when no manifest is provided for a non-builtin backend."""


def canonical_json_payload(*, meta: dict[str, Any], backends: list[Any]) -> bytes:
    """Return the canonical-JSON bytes that are signed by the issuer.

    Uses ``sort_keys=True`` and the most compact separators so the signer
    and the verifier agree byte-for-byte. Only ``meta`` + ``backends`` are
    included — the ``signature`` table is never self-referentially signed.
    """
    return json.dumps(
        {"meta": meta, "backends": backends},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    """Read + parse the TOML manifest, raising on any I/O or decode failure."""
    if not manifest_path.exists():
        raise BackendSignatureError(f"allowed_backends manifest not found: {manifest_path}")

    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise BackendSignatureError(f"Cannot read manifest {manifest_path}: {exc}") from exc

    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise BackendSignatureError(f"Manifest {manifest_path} has invalid TOML: {exc}") from exc


def _extract_signed_parts(
    data: dict[str, Any],
) -> tuple[dict[str, Any], list[Any], bytes, str]:
    """Validate manifest structure and return (meta, backends, sig_bytes, issuer_did)."""
    meta = data.get("meta")
    backends = data.get("backends")
    sig_block = data.get("signature")

    if not isinstance(meta, dict):
        raise BackendSignatureError("Manifest missing required [meta] table")
    if not isinstance(backends, list) or not backends:
        raise BackendSignatureError("Manifest missing required [[backends]] array (or empty)")
    if not isinstance(sig_block, dict):
        raise BackendSignatureError("Manifest missing required [signature] table")

    issuer_did = meta.get("issuer_did")
    if not isinstance(issuer_did, str) or not issuer_did:
        raise BackendSignatureError("Manifest [meta].issuer_did is missing or empty")

    algorithm = sig_block.get("algorithm")
    if algorithm != "ed25519":
        raise BackendSignatureError(
            f"Unsupported manifest signature algorithm: {algorithm!r} (expected 'ed25519')"
        )

    sig_b64 = sig_block.get("signature")
    if not isinstance(sig_b64, str):
        raise BackendSignatureError("Manifest [signature].signature is missing")

    try:
        sig_bytes = base64.b64decode(sig_b64, validate=True)
    except ValueError as exc:
        raise BackendSignatureError(
            f"Manifest [signature].signature is not valid base64: {exc}"
        ) from exc

    return meta, backends, sig_bytes, issuer_did


def _verify_signature(
    payload: bytes,
    sig_bytes: bytes,
    issuer_did: str,
    *,
    manifest_path: Path,
    trust_dir: Path | None,
    sink: Any | None,
) -> None:
    """Ed25519-verify ``payload`` against the issuer's trust-store pubkey.

    Fail-closed: any resolution or verification failure emits a sig-invalid
    audit event and raises :class:`BackendSignatureError`. This is the single
    isolated crypto step.
    """
    try:
        from arctrust.trust_store import (
            TrustStoreError,
            load_issuer_pubkey,
        )
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey
    except ImportError as exc:  # pragma: no cover — arctrust is a required dep
        raise BackendSignatureError(f"PyNaCl / arctrust trust store not available: {exc}") from exc

    try:
        pubkey = load_issuer_pubkey(issuer_did, trust_dir=trust_dir)
    except TrustStoreError as exc:
        emit_sig_invalid(
            manifest_path=manifest_path,
            reason=f"trust_store:{exc.code}",
            issuer_did=issuer_did,
            sink=sink,
        )
        raise BackendSignatureError(
            f"Cannot resolve issuer pubkey for {issuer_did!r}: [{exc.code}] {exc.message}"
        ) from exc

    try:
        VerifyKey(pubkey).verify(payload, sig_bytes)
    except BadSignatureError as exc:
        emit_sig_invalid(
            manifest_path=manifest_path,
            reason="bad_signature",
            issuer_did=issuer_did,
            sink=sink,
        )
        raise BackendSignatureError(
            f"Manifest signature did not verify against issuer {issuer_did!r}"
        ) from exc


def _build_verified_map(backends: list[Any]) -> dict[str, dict[str, Any]]:
    """Build the name/module-keyed map from validated backend entries."""
    verified: dict[str, dict[str, Any]] = {}
    for entry in backends:
        if not isinstance(entry, dict):
            raise BackendSignatureError("Each [[backends]] entry must be a TOML table")
        name = entry.get("name")
        module = entry.get("module")
        content_hash = entry.get("content_hash")
        if not isinstance(name, str) or not isinstance(module, str):
            raise BackendSignatureError(
                "Each [[backends]] entry requires string 'name' and 'module' fields"
            )
        if not isinstance(content_hash, str):
            raise BackendSignatureError(f"Backend {name!r}: missing 'content_hash' field")
        verified[name] = {
            "name": name,
            "module": module,
            "content_hash": content_hash,
        }
        # Also register by the dotted module path for compatibility with
        # call sites that pass the full path as ``name``.
        verified[module] = verified[name]

    return verified


def verify_allowed_backends_signature(
    manifest_path: Path,
    *,
    federal: bool,
    trust_dir: Path | None = None,
    sink: Any | None = None,
) -> dict[str, dict[str, Any]]:
    """Verify the Ed25519 signature on an ``allowed_backends`` TOML manifest.

    Loads the manifest, reconstructs the canonical-JSON signed payload, and
    verifies the signature against the issuer DID's pubkey from the trust
    store. On success returns a dict keyed by backend ``name`` with the
    full backend entry (including ``module`` and ``content_hash``).
    """
    data = _load_manifest(manifest_path)
    meta, backends, sig_bytes, issuer_did = _extract_signed_parts(data)
    payload = canonical_json_payload(meta=meta, backends=backends)
    _verify_signature(
        payload,
        sig_bytes,
        issuer_did,
        manifest_path=manifest_path,
        trust_dir=trust_dir,
        sink=sink,
    )
    emit_sig_verified(manifest_path=manifest_path, issuer_did=issuer_did, sink=sink)
    return _build_verified_map(backends)


def verify_backend_content_hash(
    name: str,
    verified: dict[str, dict[str, Any]],
    *,
    sink: Any | None = None,
) -> None:
    """Compute sha256 of the backend's module file and compare to manifest.

    The manifest ``content_hash`` is prefixed with ``sha256:`` followed by
    64 hex chars. The backend module is located by importing the first
    segment of the dotted path and hashing the ``module.__file__``. This
    catches a tampered or swapped wheel even if the rest of the manifest
    would otherwise match.
    """
    entry = verified[name]
    expected = entry["content_hash"]
    module_path = entry["module"]

    if not isinstance(expected, str) or not expected.startswith("sha256:"):
        raise BackendSignatureError(f"Backend {name!r}: content_hash must start with 'sha256:'")
    expected_hex = expected.split(":", 1)[1].strip().lower()

    if ":" in module_path:
        module_dotted, _ = module_path.rsplit(":", 1)
    else:
        module_dotted, _ = module_path.rsplit(".", 1)

    try:
        module = importlib.import_module(module_dotted)
    except ImportError as exc:
        raise BackendSignatureError(
            f"Cannot import backend module {module_dotted!r} for content_hash check: {exc}"
        ) from exc

    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise BackendSignatureError(
            f"Backend module {module_dotted!r} has no __file__ attribute; "
            "cannot verify content_hash."
        )

    try:
        actual_bytes = Path(module_file).read_bytes()
    except OSError as exc:
        raise BackendSignatureError(
            f"Cannot read backend module file {module_file}: {exc}"
        ) from exc

    actual_hex = hashlib.sha256(actual_bytes).hexdigest().lower()
    if actual_hex != expected_hex:
        emit_content_mismatch(name=name, expected=expected_hex, actual=actual_hex, sink=sink)
        raise BackendSignatureError(
            f"Backend {name!r}: content_hash mismatch.  "
            f"Manifest expected sha256:{expected_hex} but module file "
            f"{module_file} hashes to sha256:{actual_hex}.  Refusing to load."
        )
