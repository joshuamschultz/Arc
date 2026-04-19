"""Backend discovery loader — 3-tier federal-aware resolution.

Discovery order
---------------
1. Built-ins — "local" and "docker" imported directly from arcrun.backends.
   Always trusted; never loaded via entry_points.
2. Explicit config — dotted import path (e.g. "arc_backends_ssh:SSHBackend").
   Loader imports the path and verifies isinstance(obj, ExecutorBackend).
3. setuptools entry_points (group "arcrun.executor_backends") — DISABLED at
   federal tier.  At federal tier an attempt raises FederalBackendPolicyError.

Federal signature verification (M3 gap-close)
---------------------------------------------
At federal tier, explicit-config backends (dotted paths) must appear in a
signed ``allowed_backends`` manifest TOML file.  The manifest layout is::

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

Any step failure → ``BackendSignatureError``.  Federal deployments MUST pass
a signed manifest path; unsigned ``allowed_backends`` dicts are NOT accepted
at federal tier (SPEC-018 HIGH-3, fail-closed).

Audit events
------------
Every load attempt emits one of:

    executor.backend.loaded           — instance constructed and validated
    executor.backend.denied           — policy gate refused
    backend.signature_verified        — manifest signature verified
    backend.signature_invalid         — manifest signature rejected
    backend.content_hash_mismatch     — backend file did not match manifest

These flow through arcrun's event bus when one is provided, or are logged via
``logging`` when not (e.g. in tests).
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


class FederalBackendPolicyError(RuntimeError):
    """Raised when entry_points discovery is attempted at federal tier.

    Also raised when a short alias (not a dotted import path) is requested
    at federal tier and cannot be resolved from built-ins, because the only
    remaining resolution strategy (entry_points) is disabled.
    """


class BackendSignatureError(RuntimeError):
    """Raised when a backend is not in the federal allowed_backends manifest,
    or when the manifest signature / content hashes fail verification."""


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
        Legacy mapping of name → dotted import path.  Accepted at non-federal
        tiers and for test harnesses.  At federal tier this parameter is
        IGNORED — the signed ``manifest_path`` is authoritative and an unsigned
        dict is never accepted (fail-closed per SPEC-018 HIGH-3).
    manifest_path:
        Path to a signed ``allowed_backends`` TOML manifest.  REQUIRED at
        federal tier for any non-built-in backend.  The manifest is verified
        before any third-party module is imported.
    trust_dir:
        Optional override for the issuer trust store.

    Returns
    -------
    ExecutorBackend
        A verified backend instance.

    Raises
    ------
    FederalBackendPolicyError
        If entry_points discovery is attempted at federal tier, or if a short
        alias that is not a built-in is requested at federal tier.
    BackendSignatureError
        If federal tier is requested without a signed manifest (manifest_path
        is None), or if the manifest signature, issuer DID, or a backend
        content hash does not verify, or if the backend is not in the manifest.
    ValueError
        If the backend name cannot be resolved or does not implement the Protocol.
    """
    # --- Tier 1: built-ins (always trusted at all tiers) ---
    backend = _try_builtin(name)
    if backend is not None:
        _audit_loaded(name, tier=tier, path="<builtin>")
        return backend

    # At this point the name is NOT a built-in.
    is_dotted_path = ":" in name or "." in name

    if tier == "federal":
        if not is_dotted_path:
            # Short alias at federal tier: the only resolution path left is
            # entry_points, which is disabled.  Raise immediately.
            _audit_denied(
                name, tier=tier, reason="entry_points disabled at federal tier"
            )
            raise FederalBackendPolicyError(
                f"Backend '{name}' is not a built-in and entry_points discovery is "
                "disabled at federal tier.  Supply the full dotted import path "
                "(e.g. 'mypackage:MyBackend') and add it to the allowed_backends manifest."
            )
        # Federal tier: a signed manifest is mandatory.  Unsigned dicts are
        # rejected (fail-closed).  There is no warning-and-proceed fallback;
        # the risk of silently trusting an unsigned payload at federal tier is
        # too high (OWASP LLM03 / ASI04, SPEC-018 HIGH-3).
        if manifest_path is None:
            _audit_denied(name, tier="federal", reason="manifest_path required at federal tier")
            raise BackendSignatureError(
                "Federal tier requires signed manifest; unsigned dict not accepted.  "
                f"Supply manifest_path= pointing to a signed allowed_backends TOML.  "
                f"Backend '{name}' cannot be loaded without one."
            )
        verified = _verify_allowed_backends_signature(
            manifest_path=manifest_path,
            federal=True,
            trust_dir=trust_dir,
        )
        _enforce_manifest_contains(name, verified)
        _verify_backend_content_hash(name, verified)

    # --- Tier 2: explicit dotted import path ---
    if is_dotted_path:
        backend = _load_dotted(name)
        _audit_loaded(name, tier=tier, path=name)
        return backend

    # --- Tier 3: entry_points (non-federal tiers only) ---
    backend = _try_entry_point(name)
    if backend is not None:
        _audit_loaded(name, tier=tier, path=f"<entry_points:{name}>")
        return backend

    _audit_denied(name, tier=tier, reason="not found")
    raise ValueError(
        f"Backend '{name}' not found.  Built-ins: local, docker.  "
        "For third-party backends supply the dotted import path "
        "(e.g. 'mypackage.backends:MyBackend') or install a package that "
        "registers an 'arcrun.executor_backends' entry point."
    )


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
        raise ValueError(
            f"Could not instantiate backend class '{class_name}': {exc}"
        ) from exc

    if not isinstance(instance, ExecutorBackend):
        raise ValueError(
            f"'{path}' does not implement ExecutorBackend Protocol.  "
            "Ensure the class has: name, capabilities, run(), stream(), cancel(), close()."
        )
    return instance


def _try_entry_point(name: str) -> ExecutorBackend | None:
    """Attempt to load via setuptools entry_points group 'arcrun.executor_backends'."""
    try:
        from importlib.metadata import entry_points

        eps = entry_points(group="arcrun.executor_backends")
        for ep in eps:
            if ep.name == name:
                cls = ep.load()
                instance: Any = cls()
                if isinstance(instance, ExecutorBackend):
                    return instance
                logger.warning(
                    "Entry point '%s' loaded class '%s' which does not implement "
                    "ExecutorBackend Protocol; skipping.",
                    name,
                    cls,
                )
    except Exception as exc:
        logger.warning("entry_points discovery for '%s' failed: %s", name, exc)
    return None


# ---------------------------------------------------------------------------
# Federal manifest verification (Ed25519 + content hash)
# ---------------------------------------------------------------------------


def _verify_allowed_backends_signature(
    manifest_path: Path,
    *,
    federal: bool,
    trust_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Verify the Ed25519 signature on an ``allowed_backends`` TOML manifest.

    Loads the manifest, reconstructs the canonical-JSON signed payload, and
    verifies the signature against the issuer DID's pubkey from the trust
    store.  On success returns a dict keyed by backend ``name`` with the
    full backend entry (including ``module`` and ``content_hash``).

    Args:
        manifest_path: Path to the signed ``allowed_backends.toml`` file.
        federal:       Informational flag.  When False the verification is
                       still performed (fail-closed on bad signatures at every
                       tier once a signed manifest is supplied).
        trust_dir:     Optional override for the issuer trust store.

    Raises:
        BackendSignatureError: Any step of verification failed.
    """
    if not manifest_path.exists():
        raise BackendSignatureError(
            f"allowed_backends manifest not found: {manifest_path}"
        )

    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise BackendSignatureError(
            f"Cannot read manifest {manifest_path}: {exc}"
        ) from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise BackendSignatureError(
            f"Manifest {manifest_path} has invalid TOML: {exc}"
        ) from exc

    meta = data.get("meta")
    backends = data.get("backends")
    sig_block = data.get("signature")

    if not isinstance(meta, dict):
        raise BackendSignatureError("Manifest missing required [meta] table")
    if not isinstance(backends, list) or not backends:
        raise BackendSignatureError(
            "Manifest missing required [[backends]] array (or empty)"
        )
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
        raise BackendSignatureError(
            f"PyNaCl / arctrust trust store not available: {exc}"
        ) from exc

    try:
        pubkey = load_issuer_pubkey(issuer_did, trust_dir=trust_dir)
    except TrustStoreError as exc:
        _audit_sig_invalid(
            manifest_path=manifest_path,
            reason=f"trust_store:{exc.code}",
            issuer_did=issuer_did,
        )
        raise BackendSignatureError(
            f"Cannot resolve issuer pubkey for {issuer_did!r}: "
            f"[{exc.code}] {exc.message}"
        ) from exc

    try:
        VerifyKey(pubkey).verify(payload, sig_bytes)
    except BadSignatureError as exc:
        _audit_sig_invalid(
            manifest_path=manifest_path,
            reason="bad_signature",
            issuer_did=issuer_did,
        )
        raise BackendSignatureError(
            f"Manifest signature did not verify against issuer {issuer_did!r}"
        ) from exc

    _audit_sig_verified(manifest_path=manifest_path, issuer_did=issuer_did)

    # Build the verified mapping: name → entry dict
    verified: dict[str, dict[str, Any]] = {}
    for entry in backends:
        if not isinstance(entry, dict):
            raise BackendSignatureError(
                "Each [[backends]] entry must be a TOML table"
            )
        name = entry.get("name")
        module = entry.get("module")
        content_hash = entry.get("content_hash")
        if not isinstance(name, str) or not isinstance(module, str):
            raise BackendSignatureError(
                "Each [[backends]] entry requires string 'name' and 'module' fields"
            )
        if not isinstance(content_hash, str):
            raise BackendSignatureError(
                f"Backend {name!r}: missing 'content_hash' field"
            )
        verified[name] = {
            "name": name,
            "module": module,
            "content_hash": content_hash,
        }
        # Also register by the dotted module path for compatibility with
        # existing call sites that pass the full path as ``name``.
        verified[module] = verified[name]

    return verified


def _enforce_manifest_contains(name: str, verified: dict[str, dict[str, Any]]) -> None:
    """Raise BackendSignatureError if ``name`` is not in the verified mapping."""
    if name in verified:
        return
    _audit_denied(name, tier="federal", reason="not in signed manifest")
    raise BackendSignatureError(
        f"Backend '{name}' is not in the signed allowed_backends manifest."
    )


def _verify_backend_content_hash(
    name: str, verified: dict[str, dict[str, Any]]
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
        raise BackendSignatureError(
            f"Backend {name!r}: content_hash must start with 'sha256:'"
        )
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
        _audit_content_mismatch(name=name, expected=expected_hex, actual=actual_hex)
        raise BackendSignatureError(
            f"Backend {name!r}: content_hash mismatch.  "
            f"Manifest expected sha256:{expected_hex} but module file "
            f"{module_file} hashes to sha256:{actual_hex}.  Refusing to load."
        )


def _canonical_json_payload(
    *, meta: dict[str, Any], backends: list[Any]
) -> bytes:
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
    Note: this helper is no longer called from ``load_backend`` at federal tier —
    federal tier now hard-fails if no signed manifest is provided (SPEC-018 HIGH-3).
    Non-federal callers and tests may still invoke this directly.
    """
    if allowed_backends is None:
        _audit_denied(name, tier="federal", reason="no allowed_backends manifest provided")
        raise BackendSignatureError(
            f"Federal tier requires an allowed_backends manifest.  "
            f"Backend '{name}' cannot be loaded without one."
        )

    if name not in allowed_backends:
        _audit_denied(
            name, tier="federal", reason=f"'{name}' not in allowed_backends manifest"
        )
        raise BackendSignatureError(
            f"Backend '{name}' is not in the allowed_backends manifest.  "
            "Add it under [[executor.allowed_backends]] in arcrun.toml."
        )

    logger.debug(
        "Federal manifest (unsigned path): backend '%s' is listed.  "
        "For production federal use, switch to a signed manifest via "
        "load_backend(manifest_path=...).",
        name,
    )


# ---------------------------------------------------------------------------
# Audit helpers (emit to logger; wired to OTel in production)
# ---------------------------------------------------------------------------


def _audit_loaded(name: str, *, tier: str, path: str) -> None:
    logger.info(
        "executor.backend.loaded name=%s tier=%s path=%s",
        name,
        tier,
        path,
    )


def _audit_denied(name: str, *, tier: str, reason: str) -> None:
    logger.warning(
        "executor.backend.denied name=%s tier=%s reason=%s",
        name,
        tier,
        reason,
    )


def _audit_sig_verified(*, manifest_path: Path, issuer_did: str) -> None:
    logger.info(
        "backend.signature_verified manifest=%s issuer_did=%s",
        manifest_path,
        issuer_did,
    )


def _audit_sig_invalid(
    *, manifest_path: Path, reason: str, issuer_did: str | None = None
) -> None:
    logger.warning(
        "backend.signature_invalid manifest=%s issuer_did=%s reason=%s",
        manifest_path,
        issuer_did,
        reason,
    )


def _audit_content_mismatch(*, name: str, expected: str, actual: str) -> None:
    logger.warning(
        "backend.content_hash_mismatch name=%s expected=sha256:%s actual=sha256:%s",
        name,
        expected,
        actual,
    )
