"""Audit emitters for the bundle lifecycle — one call site per decided outcome.

Every event is emitted from inside arcbundle, at the point the outcome is
actually decided, never from whichever surface happened to ask. A CLI install, a
release-CI install, and a future UI install of the same bundle therefore produce
the same record, and no second caller can invent a different event for the same
fact.

Shape follows ``arcrun/backends/_audit.py`` — the same concern one layer up.
Each helper logs unconditionally and additionally calls ``arctrust.audit.emit``
when a sink is supplied; sinks fan out from that single point unchanged.
Failures inside the audit path are swallowed and logged, so auditing can never
break the install it audits (NIST AU-5).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from arctrust import AuditEvent, emit

__all__ = [
    "UNKNOWN",
    "emit_bundle_verified",
    "emit_content_mismatch",
    "emit_installed",
    "emit_removed",
    "emit_signature_invalid",
]

_logger = logging.getLogger("arcbundle.audit")

# Recorded when no operator identity reached this layer. Naming the installing
# subsystem is honest; attributing an unattended install to a person is not.
_INSTALLER_ACTOR = "did:arc:system:module-installer"

#: Stand-in for a field a refusal could not establish. A manifest that never
#: parsed has no module name and no issuer, and recording that plainly beats
#: dropping the key and leaving an auditor to guess why it is missing.
UNKNOWN = "unknown"


def _emit(
    *,
    action: str,
    target: str,
    outcome: str,
    extra: dict[str, Any],
    sink: Any | None,
    actor_did: str | None,
    tier: str | None = None,
) -> None:
    """Emit one AuditEvent through the single ``arctrust.audit.emit`` point."""
    if sink is None:
        return
    try:
        emit(
            AuditEvent(
                actor_did=actor_did or _INSTALLER_ACTOR,
                action=action,
                target=target,
                outcome=outcome,
                tier=tier,
                extra=extra,
            ),
            sink,
        )
    except Exception:  # reason: fail-open — auditing never breaks the audited action (AU-5)
        _logger.warning(
            "failed to emit AuditEvent action=%s target=%s — swallowing (AU-5)",
            action,
            target,
            exc_info=True,
        )


def emit_bundle_verified(
    *,
    bundle: Path,
    module: str,
    issuer: str,
    version: str,
    tier: str,
    files: int,
    sink: Any | None,
    actor_did: str | None = None,
) -> None:
    """Emit ``module.bundle.verified`` — every gate passed, nothing written yet."""
    _logger.info(
        "module.bundle.verified module=%s version=%s issuer=%s tier=%s files=%d bundle=%s",
        module,
        version,
        issuer,
        tier,
        files,
        bundle,
    )
    _emit(
        action="module.bundle.verified",
        target=module,
        outcome="allow",
        tier=tier,
        extra={"bundle": str(bundle), "issuer": issuer, "version": version, "files": files},
        sink=sink,
        actor_did=actor_did,
    )


def emit_signature_invalid(
    *,
    bundle: Path,
    module: str,
    issuer: str,
    reason: str,
    tier: str,
    sink: Any | None,
    actor_did: str | None = None,
) -> None:
    """Emit ``module.signature_invalid`` — untrusted issuer, wrong tier, or bad signature."""
    _logger.warning(
        "module.signature_invalid module=%s issuer=%s tier=%s bundle=%s reason=%s",
        module,
        issuer,
        tier,
        bundle,
        reason,
    )
    _emit(
        action="module.signature_invalid",
        target=module,
        outcome="deny",
        tier=tier,
        extra={"bundle": str(bundle), "issuer": issuer, "reason": reason},
        sink=sink,
        actor_did=actor_did,
    )


def emit_content_mismatch(
    *,
    bundle: Path,
    module: str,
    issuer: str,
    reason: str,
    tier: str,
    sink: Any | None,
    actor_did: str | None = None,
) -> None:
    """Emit ``module.content_hash_mismatch`` — a payload file is altered or undeclared."""
    _logger.warning(
        "module.content_hash_mismatch module=%s issuer=%s tier=%s bundle=%s reason=%s",
        module,
        issuer,
        tier,
        bundle,
        reason,
    )
    _emit(
        action="module.content_hash_mismatch",
        target=module,
        outcome="deny",
        tier=tier,
        extra={"bundle": str(bundle), "issuer": issuer, "reason": reason},
        sink=sink,
        actor_did=actor_did,
    )


def emit_installed(
    *,
    bundle: Path,
    module: str,
    issuer: str,
    version: str,
    path: Path,
    files: int,
    sink: Any | None,
    actor_did: str | None = None,
) -> None:
    """Emit ``module.installed`` — a verified bundle is now on disk at ``path``."""
    _logger.info(
        "module.installed module=%s version=%s issuer=%s files=%d path=%s bundle=%s",
        module,
        version,
        issuer,
        files,
        path,
        bundle,
    )
    _emit(
        action="module.installed",
        target=module,
        outcome="allow",
        extra={
            "bundle": str(bundle),
            "issuer": issuer,
            "version": version,
            "files": files,
            "path": str(path),
        },
        sink=sink,
        actor_did=actor_did,
    )


def emit_removed(
    *,
    module: str,
    path: Path,
    sink: Any | None,
    actor_did: str | None = None,
) -> None:
    """Emit ``module.removed`` — the materialized tree is gone.

    A materialized tree records no provenance, so there is no bundle or issuer
    left to name by the time it is deleted. Recording the module and the path
    that was emptied is the whole truth this operation has.
    """
    _logger.info("module.removed module=%s path=%s", module, path)
    _emit(
        action="module.removed",
        target=module,
        outcome="allow",
        extra={"path": str(path)},
        sink=sink,
        actor_did=actor_did,
    )
