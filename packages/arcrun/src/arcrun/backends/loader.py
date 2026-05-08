"""Backend discovery loader — 3-tier federal-aware resolution.

Discovery order
---------------
1. Built-ins — "local" and "docker" imported directly from arcrun.backends.
   Always trusted; never require a manifest.
2. Explicit config — dotted import path (e.g. "arc_backends_ssh:SSHBackend").
   Loader imports the path and verifies isinstance(obj, ExecutorBackend).
   A signed manifest is REQUIRED at ALL tiers (Phase C, see policy.py).

Entry-points are PERMANENTLY DISABLED at all tiers (Phase C supply-chain
lockdown). See policy.py for the design rationale.

Signing requirement (Phase C, all tiers)
-----------------------------------------
Every non-builtin backend requires a signed ``allowed_backends`` manifest.
The tier stringency knob determines *which issuers are trusted*, not
*whether to verify*. The manifest layout, canonical-JSON encoding, and
Ed25519 signature pipeline live in ``_verifier.py``; the per-name
membership check lives in ``_manifest.py``; audit-event emission lives in
``_audit.py``.

Audit events
------------
Every load attempt emits one AuditEvent via ``arctrust.audit.emit()``:

    executor.backend.loaded           — instance constructed and validated
    executor.backend.denied           — policy gate refused
    backend.signature_verified        — manifest signature verified
    backend.signature_invalid         — manifest signature rejected
    backend.content_hash_mismatch     — backend file did not match manifest

When ``audit_sink`` is None the events are logged via ``logging`` only.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from arcrun.backends._audit import emit_denied, emit_loaded
from arcrun.backends._manifest import enforce_manifest_contains
from arcrun.backends._verifier import (
    BackendSignatureError,
    verify_allowed_backends_signature,
    verify_backend_content_hash,
)
from arcrun.backends.base import ExecutorBackend

__all__ = [
    "BackendSignatureError",
    "FederalBackendPolicyError",
    "load_backend",
]

logger = logging.getLogger(__name__)


class FederalBackendPolicyError(RuntimeError):
    """Raised when entry_points discovery is attempted at any tier (Phase C).

    Entry-points are permanently disabled. Also raised when a short alias
    (not a dotted import path) is requested and cannot be resolved from
    built-ins.
    """


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
        Backend identifier. Either a short alias ("local", "docker") or a
        dotted import path ("mypackage.backends:MyBackend").
    tier:
        Deployment tier from config.security.tier. One of
        "federal", "enterprise", "personal".
    allowed_backends:
        Legacy mapping of name → dotted import path. IGNORED at all tiers
        (Phase C: signed manifest is always authoritative).
    manifest_path:
        Path to a signed ``allowed_backends`` TOML manifest. REQUIRED for
        any non-builtin backend at all tiers. The manifest is verified
        before any third-party module is imported.
    trust_dir:
        Optional override for the issuer trust store.
    audit_sink:
        Optional ``arctrust.AuditSink`` implementation. When provided,
        AuditEvents are emitted for every load outcome in addition to logger
        output. When None, falls back to logger-only.

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
        emit_loaded(name, tier=tier, path="<builtin>", sink=audit_sink)
        return backend

    is_dotted_path = ":" in name or "." in name

    if not is_dotted_path:
        # Short alias at any tier: entry-points are permanently disabled.
        emit_denied(
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

    # All non-builtin backends require a signed manifest at every tier
    # (fail-closed per SPEC-018 HIGH-3, extended to all tiers in Phase C).
    if manifest_path is None:
        emit_denied(
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

    verified = verify_allowed_backends_signature(
        manifest_path=manifest_path,
        federal=(tier == "federal"),
        trust_dir=trust_dir,
        sink=audit_sink,
    )
    enforce_manifest_contains(name, verified, tier=tier, sink=audit_sink)
    verify_backend_content_hash(name, verified, sink=audit_sink)

    backend = _load_dotted(name)
    emit_loaded(name, tier=tier, path=name, sink=audit_sink)
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
    except Exception as exc:  # reason: re-raise after log
        raise ValueError(f"Could not instantiate backend class '{class_name}': {exc}") from exc

    if not isinstance(instance, ExecutorBackend):
        raise ValueError(
            f"'{path}' does not implement ExecutorBackend Protocol.  "
            "Ensure the class has: name, capabilities, run(), stream(), cancel(), close()."
        )
    return instance


# Test-import aliases. Tests reach into the verification helpers directly via
# their original underscore names; re-export so the existing import paths
# resolve without forcing every test to retarget.
_verify_allowed_backends_signature = verify_allowed_backends_signature
_verify_backend_content_hash = verify_backend_content_hash
_enforce_manifest_contains = enforce_manifest_contains
