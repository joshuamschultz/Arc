"""Backend discovery loader — 3-tier federal-aware resolution.

Discovery order
---------------
1. Built-ins — "local" and "docker" imported directly from arcrun.backends.
   Always trusted; never require a manifest.
2. Explicit config — dotted import path (e.g. "arc_backends_ssh:SSHBackend").
   Loader imports the path and verifies isinstance(obj, ExecutorBackend).
   A signed manifest is REQUIRED at ALL tiers (Phase C, see policy.py).

Entry-points are PERMANENTLY DISABLED at all tiers (Phase C supply-chain
lockdown).  See policy.py for the design rationale.

Signing requirement (Phase C, all tiers)
-----------------------------------------
Every non-builtin backend requires a signed ``allowed_backends`` manifest.
The tier stringency knob determines *which issuers are trusted*, not
*whether to verify*.  The manifest layout is::

    [meta]
    issued_at = "2026-04-18T00:00:00Z"
    issuer_did = "did:arc:org:trust-authority/abcd1234"

    [[backends]]
    name = "ssh"
    module = "arc_backend_ssh:SSHBackend"
    content_hash = "sha256:<HEX_OF_MODULE_FILE>"

    [signature]
    algorithm = "ed25519"
    signature = "<BASE64_SIG_OVER_CANONICAL_JSON_OF_meta+backends>"

The loader:
  1. Reads the TOML manifest with ``tomllib``.
  2. Builds canonical JSON of ``{"meta": ..., "backends": [...]}`` with
     ``sort_keys=True`` and compact separators.
  3. Resolves the issuer pubkey via
     ``arctrust.trust_store.load_issuer_pubkey(meta.issuer_did)``.
  4. Verifies the Ed25519 signature with PyNaCl.
  5. For each backend, computes ``sha256`` of the imported module's source
     file and compares against ``content_hash`` (algorithm prefix ``sha256:``).

Any step failure → ``BackendSignatureError``.  All tiers MUST pass
a signed manifest for non-builtin backends; unsigned ``allowed_backends``
dicts are NOT accepted (fail-closed per SPEC-018 HIGH-3).

Audit events
------------
Every load attempt emits one AuditEvent via ``arctrust.audit.emit()``:

    executor.backend.loaded           — instance constructed and validated
    executor.backend.denied           — policy gate refused
    backend.signature_verified        — manifest signature verified
    backend.signature_invalid         — manifest signature rejected
    backend.content_hash_mismatch     — backend file did not match manifest

When ``audit_sink`` is None the events are logged via ``logging`` only
(backwards-compatible fallback).
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import logging
import tomllib
from pathlib import Path
from typing import Any

from arcrun.backends.base import ExecutorBackend

logger = logging.getLogger(__name__)

# Sentinel actor DID used when no per-request identity is available
_LOADER_ACTOR = "did:arc:system:backend-loader"


class FederalBackendPolicyError(RuntimeError):
    """Raised when entry_points discovery is attempted at any tier (Phase C).

    Entry-points are permanently disabled.  Also raised when a short alias
    (not a dotted import path) is requested and cannot be resolved from
    built-ins.
    """


class BackendSignatureError(RuntimeError):
    """Raised when a backend is not in the allowed_backends manifest,
    or when the manifest signature / content hashes fail verification,
    or when no manifest is provided for a non-builtin backend."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_backend(
    name: str,
    *,
    tier: str = "personal",
    allowed_backends: dict[str, str] | None = None,
    manifest_path: Path | None = None,
    trust_dir: Path | None = None,
    audit_sink: Any | None = None,
) -> ExecutorBackend:
    """Resolve and return an ExecutorBackend by name.

    Parameters
    ----------
    name:
        Backend identifier.  Either a short alias ("local", "docker") or a
        dotted import path ("mypackage.backends:MyBackend").
    tier:
        Deployment tier from config.security.tier.  One of
        "federal", "enterprise", "personal".
    allowed_backends:
        Legacy mapping of name → dotted import path.  IGNORED at all tiers
        (Phase C: signed manifest is always authoritative).
    manifest_path:
        Path to a signed ``allowed_backends`` TOML manifest.  REQUIRED for
        any non-builtin backend at all tiers.  The manifest is verified
        before any third-party module is imported.
    trust_dir:
        Optional override for the issuer trust store.
    audit_sink:
        Optional ``arctrust.AuditSink`` implementation. When provided,
        AuditEvents are emitted for every load outcome in addition to logger
        output.  When None, falls back to logger-only (backwards compatible).

    Returns
    -------
    ExecutorBackend
        A verified backend instance.

    Raises
    ------
    FederalBackendPolicyError
        If a short alias that is not a built-in is requested (entry-points
        are disabled at all tiers, Phase C).
    BackendSignatureError
        If no signed manifest is provided for a non-builtin backend, or if
        the manifest signature, issuer DID, or a backend content hash does
        not verify, or if the backend is not in the manifest.
    ValueError
        If the backend name cannot be resolved or does not implement the
        Protocol.
    """
    # --- Tier 1: built-ins (always trusted at all tiers; no manifest needed) ---
    backend = _try_builtin(name)
    if backend is not None:
        _emit_loaded(name, tier=tier, path="<builtin>", sink=audit_sink)
        return backend

    # At this point the name is NOT a built-in.
    is_dotted_path = ":" in name or "." in name

    if not is_dotted_path:
        # Short alias at any tier: entry-points are permanently disabled.
        # This path previously fell through to _try_entry_point at non-federal
        # tiers — Phase C closes that bypass.
        _emit_denied(
            name,
            tier=tier,
            reason="entry_points disabled at all tiers (Phase C supply-chain lockdown)",
            sink=audit_sink,
        )
        raise FederalBackendPolicyError(
            f"Backend '{name}' is not a built-in and entry_points discovery is "
            "permanently disabled at all tiers (Phase C).  "
            "Supply the full dotted import path "
            "(e.g. 'mypackage:MyBackend') and add it to a signed allowed_backends manifest."
        )

    # All non-builtin backends require a signed manifest at every tier.
    # Unsigned dicts are never accepted (fail-closed per SPEC-018 HIGH-3,
    # extended to all tiers in Phase C).
    if manifest_path is None:
        _emit_denied(
            name,
            tier=tier,
            reason="manifest_path required for non-builtin backends at all tiers (Phase C)",
            sink=audit_sink,
        )
        raise BackendSignatureError(
            "All tiers require a signed manifest for non-builtin backends (Phase C).  "
            f"Supply manifest_path= pointing to a signed allowed_backends TOML.  "
            f"Backend '{name}' cannot be loaded without one."
        )

    verified = _verify_allowed_backends_signature(
        manifest_path=manifest_path,
        federal=(tier == "federal"),
        trust_dir=trust_dir,
        sink=audit_sink,
    )
    _enforce_manifest_contains(name, verified, tier=tier, sink=audit_sink)
    _verify_backend_content_hash(name, verified, sink=audit_sink)

    # --- Load the verified dotted import path ---
    backend = _load_dotted(name)
    _emit_loaded(name, tier=tier, path=name, sink=audit_sink)
    return backend


# ---------------------------------------------------------------------------
# Internal resolvers
# ---------------------------------------------------------------------------


def _try_builtin(name: str) -> ExecutorBackend | None:
    """Return a built-in backend instance for known aliases, else None."""
    if name == "local":
        from arcrun.backends.local import LocalBackend

        return LocalBackend()
    if name == "docker":
        from arcrun.backends.docker import DockerBackend

        return DockerBackend()
    return None


def _load_dotted(path: str) -> ExecutorBackend:
    """Import 'package.module:ClassName' and return an instance.

    The separator between module path and class name is ':'.
    Falls back to splitting on the last '.' if ':' is absent.
    """
    if ":" in path:
        module_path, class_name = path.rsplit(":", 1)
    else:
        module_path, class_name = path.rsplit(".", 1)

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ValueError(f"Cannot import backend module '{module_path}': {exc}") from exc

    cls: Any = getattr(module, class_name, None)
    if cls is None:
        raise ValueError(f"Module '{module_path}' has no attribute '{class_name}'")

    try:
        instance = cls()
    except Exception as exc:
        raise ValueError(f"Could not instantiate backend class '{class_name}': {exc}") from exc

    if not isinstance(instance, ExecutorBackend):
        raise ValueError(
            f"'{path}' does not implement ExecutorBackend Protocol.  "
            "Ensure the class has: name, capabilities, run(), stream(), cancel(), close()."
        )
    return instance


# ---------------------------------------------------------------------------
# Manifest verification (Ed25519 + content hash)
# ---------------------------------------------------------------------------


def _verify_allowed_backends_signature(
    manifest_path: Path,
    *,
    federal: bool,
    trust_dir: Path | None = None,
    sink: Any | None = None,
) -> dict[str, dict[str, Any]]:
    """Verify the Ed25519 signature on an ``allowed_backends`` TOML manifest.

    Loads the manifest, reconstructs the canonical-JSON signed payload, and
    verifies the signature against the issuer DID's pubkey from the trust
    store.  On success returns a dict keyed by backend ``name`` with the
    full backend entry (including ``module`` and ``content_hash``).

    Args:
        manifest_path: Path to the signed ``allowed_backends.toml`` file.
        federal:       True when called for a federal-tier deployment.
        trust_dir:     Optional override for the issuer trust store.
        sink:          Optional AuditSink for AuditEvent emission.

    Raises:
        BackendSignatureError: Any step of verification failed.
    """
    if not manifest_path.exists():
        raise BackendSignatureError(f"allowed_backends manifest not found: {manifest_path}")

    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise BackendSignatureError(f"Cannot read manifest {manifest_path}: {exc}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise BackendSignatureError(f"Manifest {manifest_path} has invalid TOML: {exc}") from exc

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

    # Build the canonical signed payload: JSON with sort_keys + compact separators.
    payload = _canonical_json_payload(meta=meta, backends=backends)

    # Resolve issuer pubkey and verify.
    # arctrust is the shared trust-store package; no arcagent dep required.
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
        _emit_sig_invalid(
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
        _emit_sig_invalid(
            manifest_path=manifest_path,
            reason="bad_signature",
            issuer_did=issuer_did,
            sink=sink,
        )
        raise BackendSignatureError(
            f"Manifest signature did not verify against issuer {issuer_did!r}"
        ) from exc

    _emit_sig_verified(manifest_path=manifest_path, issuer_did=issuer_did, sink=sink)

    # Build the verified mapping: name → entry dict
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
        # existing call sites that pass the full path as ``name``.
        verified[module] = verified[name]

    return verified


def _enforce_manifest_contains(
    name: str,
    verified: dict[str, dict[str, Any]],
    *,
    tier: str = "unknown",
    sink: Any | None = None,
) -> None:
    """Raise BackendSignatureError if ``name`` is not in the verified mapping."""
    if name in verified:
        return
    _emit_denied(name, tier=tier, reason="not in signed manifest", sink=sink)
    raise BackendSignatureError(f"Backend '{name}' is not in the signed allowed_backends manifest.")


def _verify_backend_content_hash(
    name: str,
    verified: dict[str, dict[str, Any]],
    *,
    sink: Any | None = None,
) -> None:
    """Compute sha256 of the backend's module file and compare to manifest.

    The manifest ``content_hash`` is prefixed with ``sha256:`` followed by
    64 hex chars.  The backend module is located by importing the first
    segment of the dotted path and hashing the ``module.__file__``.  This
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
        _emit_content_mismatch(name=name, expected=expected_hex, actual=actual_hex, sink=sink)
        raise BackendSignatureError(
            f"Backend {name!r}: content_hash mismatch.  "
            f"Manifest expected sha256:{expected_hex} but module file "
            f"{module_file} hashes to sha256:{actual_hex}.  Refusing to load."
        )


def _canonical_json_payload(*, meta: dict[str, Any], backends: list[Any]) -> bytes:
    """Return the canonical-JSON bytes that are signed by the issuer.

    Uses ``sort_keys=True`` and the most compact separators so the signer
    and the verifier agree byte-for-byte.  Only ``meta`` + ``backends`` are
    included in the signed payload — the ``signature`` table is never
    self-referentially signed.
    """
    return json.dumps(
        {"meta": meta, "backends": backends},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Legacy helper kept for the existing test suite
# ---------------------------------------------------------------------------


def _enforce_federal_manifest(
    name: str,
    *,
    allowed_backends: dict[str, str] | None,
) -> None:
    """Raise unless ``name`` is in the (unsigned) allowed_backends dict.

    Preserved for unit tests that exercise the dict-based gate directly.
    Note: this helper is no longer called from ``load_backend`` — all tiers
    now hard-fail if no signed manifest is provided (Phase C, SPEC-018 HIGH-3).
    Non-federal callers and tests may still invoke this directly.
    """
    if allowed_backends is None:
        _emit_denied_log(name, tier="federal", reason="no allowed_backends manifest provided")
        raise BackendSignatureError(
            f"Federal tier requires an allowed_backends manifest.  "
            f"Backend '{name}' cannot be loaded without one."
        )

    if name not in allowed_backends:
        _emit_denied_log(name, tier="federal", reason=f"'{name}' not in allowed_backends manifest")
        raise BackendSignatureError(
            f"Backend '{name}' is not in the allowed_backends manifest.  "
            "Add it under [[executor.allowed_backends]] in arcrun.toml."
        )

    logger.debug(
        "Federal manifest (unsigned path): backend '%s' is listed.  "
        "For production use, switch to a signed manifest via "
        "load_backend(manifest_path=...).",
        name,
    )


# ---------------------------------------------------------------------------
# Audit helpers — emit AuditEvents via arctrust, fall back to logger
# ---------------------------------------------------------------------------


def _emit_loaded(name: str, *, tier: str, path: str, sink: Any | None) -> None:
    """Emit executor.backend.loaded AuditEvent."""
    logger.info(
        "executor.backend.loaded name=%s tier=%s path=%s",
        name,
        tier,
        path,
    )
    if sink is not None:
        _emit_audit_event(
            action="executor.backend.loaded",
            target=name,
            outcome="allow",
            tier=tier,
            extra={"path": path},
            sink=sink,
        )


def _emit_denied(name: str, *, tier: str, reason: str, sink: Any | None) -> None:
    """Emit executor.backend.denied AuditEvent."""
    logger.warning(
        "executor.backend.denied name=%s tier=%s reason=%s",
        name,
        tier,
        reason,
    )
    if sink is not None:
        _emit_audit_event(
            action="executor.backend.denied",
            target=name,
            outcome="deny",
            tier=tier,
            extra={"reason": reason},
            sink=sink,
        )


def _emit_denied_log(name: str, *, tier: str, reason: str) -> None:
    """Logger-only denied event used by legacy _enforce_federal_manifest."""
    logger.warning(
        "executor.backend.denied name=%s tier=%s reason=%s",
        name,
        tier,
        reason,
    )


def _emit_sig_verified(*, manifest_path: Path, issuer_did: str, sink: Any | None) -> None:
    """Emit backend.signature_verified AuditEvent."""
    logger.info(
        "backend.signature_verified manifest=%s issuer_did=%s",
        manifest_path,
        issuer_did,
    )
    if sink is not None:
        _emit_audit_event(
            action="backend.signature_verified",
            target=str(manifest_path),
            outcome="allow",
            extra={"issuer_did": issuer_did},
            sink=sink,
        )


def _emit_sig_invalid(
    *, manifest_path: Path, reason: str, issuer_did: str | None = None, sink: Any | None
) -> None:
    """Emit backend.signature_invalid AuditEvent."""
    logger.warning(
        "backend.signature_invalid manifest=%s issuer_did=%s reason=%s",
        manifest_path,
        issuer_did,
        reason,
    )
    if sink is not None:
        _emit_audit_event(
            action="backend.signature_invalid",
            target=str(manifest_path),
            outcome="deny",
            extra={"issuer_did": issuer_did, "reason": reason},
            sink=sink,
        )


def _emit_content_mismatch(*, name: str, expected: str, actual: str, sink: Any | None) -> None:
    """Emit backend.content_hash_mismatch AuditEvent."""
    logger.warning(
        "backend.content_hash_mismatch name=%s expected=sha256:%s actual=sha256:%s",
        name,
        expected,
        actual,
    )
    if sink is not None:
        _emit_audit_event(
            action="backend.content_hash_mismatch",
            target=name,
            outcome="deny",
            extra={"expected": expected, "actual": actual},
            sink=sink,
        )


def _emit_audit_event(
    *,
    action: str,
    target: str,
    outcome: str,
    tier: str | None = None,
    extra: dict[str, Any] | None = None,
    sink: Any,
) -> None:
    """Build and emit an AuditEvent to sink via arctrust.audit.emit().

    Swallows import errors gracefully so audit never breaks the load path.
    """
    try:
        from arctrust import AuditEvent, emit

        event = AuditEvent(
            actor_did=_LOADER_ACTOR,
            action=action,
            target=target,
            outcome=outcome,
            tier=tier,
            extra=extra or {},
        )
        emit(event, sink)
    except Exception:
        logger.warning(
            "Failed to emit AuditEvent action=%s target=%s — swallowing (AU-5)",
            action,
            target,
            exc_info=True,
        )
