"""Audit-event emitters for backend load and verification.

Shared between ``loader.py`` (load/deny outcomes) and ``_verifier.py``
(signature/content-hash outcomes). Each helper logs unconditionally
and additionally calls ``arctrust.audit.emit()`` when a sink is
provided. Failures inside the audit path are swallowed and logged so
audit emission can never break the load path (AU-5).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("arcrun.backends.loader")

# Sentinel actor DID used when no per-request identity is available.
_LOADER_ACTOR = "did:arc:system:backend-loader"


def _emit_audit_event(
    *,
    action: str,
    target: str,
    outcome: str,
    tier: str | None = None,
    actor_did: str | None = None,
    extra: dict[str, Any] | None = None,
    sink: Any,
) -> None:
    """Build and emit an AuditEvent to sink via arctrust.audit.emit()."""
    try:
        from arctrust import AuditEvent, emit

        event = AuditEvent(
            actor_did=actor_did or _LOADER_ACTOR,
            action=action,
            target=target,
            outcome=outcome,
            tier=tier,
            extra=extra or {},
        )
        emit(event, sink)
    except Exception:  # reason: fail-open — log + continue
        logger.warning(
            "Failed to emit AuditEvent action=%s target=%s — swallowing (AU-5)",
            action,
            target,
            exc_info=True,
        )


def emit_backend_selected(
    *,
    tier: str,
    resolved: str,
    isolation: str,
    caller_did: str | None,
    relax: str | None,
    relax_reason: str,
    platform_supports_vm: bool,
    outcome: str,
    sink: Any | None,
) -> None:
    """Emit code_exec.backend.selected on every execute_python build (AU-2/AU-3).

    Carries the AU-3 record content: caller identity, tier, resolved backend +
    isolation, the relax value and its reason, the platform VM fact, and the
    allow/refuse outcome. Emitted for successes AND fail-closed refusals.
    """
    logger.info(
        "code_exec.backend.selected tier=%s resolved=%s isolation=%s outcome=%s",
        tier,
        resolved,
        isolation,
        outcome,
    )
    if sink is not None:
        _emit_audit_event(
            action="code_exec.backend.selected",
            target=resolved,
            outcome=outcome,
            tier=tier,
            actor_did=caller_did,
            extra={
                "isolation": isolation,
                "relax": relax,
                "relax_reason": relax_reason,
                "platform_supports_vm": platform_supports_vm,
            },
            sink=sink,
        )


def emit_isolation_downgraded(
    *,
    tier: str,
    resolved: str,
    reason: str,
    caller_did: str | None,
    sink: Any | None,
) -> None:
    """Emit code_exec.isolation.downgraded when a tier-permitted downgrade occurs.

    Fires only for a personal operator's explicit relax OFF (sandbox off). A
    no-KVM host is not a downgrade for personal/enterprise (they keep their
    container floor); federal never downgrades (it refuses instead), so this
    event never carries tier=federal.
    """
    logger.warning(
        "code_exec.isolation.downgraded tier=%s resolved=%s reason=%s",
        tier,
        resolved,
        reason,
    )
    if sink is not None:
        _emit_audit_event(
            action="code_exec.isolation.downgraded",
            target=resolved,
            outcome="downgrade",
            tier=tier,
            actor_did=caller_did,
            extra={"reason": reason},
            sink=sink,
        )


def emit_loaded(name: str, *, tier: str, path: str, sink: Any | None) -> None:
    """Emit executor.backend.loaded AuditEvent."""
    logger.info("executor.backend.loaded name=%s tier=%s path=%s", name, tier, path)
    if sink is not None:
        _emit_audit_event(
            action="executor.backend.loaded",
            target=name,
            outcome="allow",
            tier=tier,
            extra={"path": path},
            sink=sink,
        )


def emit_denied(name: str, *, tier: str, reason: str, sink: Any | None) -> None:
    """Emit executor.backend.denied AuditEvent."""
    logger.warning("executor.backend.denied name=%s tier=%s reason=%s", name, tier, reason)
    if sink is not None:
        _emit_audit_event(
            action="executor.backend.denied",
            target=name,
            outcome="deny",
            tier=tier,
            extra={"reason": reason},
            sink=sink,
        )


def emit_sig_verified(*, manifest_path: Path, issuer_did: str, sink: Any | None) -> None:
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


def emit_sig_invalid(
    *,
    manifest_path: Path,
    reason: str,
    issuer_did: str | None = None,
    sink: Any | None,
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


def emit_content_mismatch(
    *,
    name: str,
    expected: str,
    actual: str,
    sink: Any | None,
) -> None:
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
