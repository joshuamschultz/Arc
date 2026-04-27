"""Tier-driven secret resolver.

Implements the three-tier credential policy defined in SDD §3.1:

    Federal   — vault required; any failure is a hard error. No env or file
                fallback, even if an env var is set.  This enforces zero-trust:
                secrets must come from a vetted vault, not from environment
                injection.

    Enterprise — try vault first; on VaultUnreachable WARN + emit an audit
                event, then fall back to the env var (if configured). If the env
                var is also missing, raise.

    Personal   — try vault if configured; otherwise try env var; otherwise
                read from ~/.arc/secrets/{name} (0600 mode enforced by the file
                backend); if all fail, raise.

The ``resolve_secret`` function is the single entry point.  Callers pass the
resolved ``VaultBackend`` (or ``None`` if unconfigured), the tier, and an
optional env-var name to use as fallback.

Audit events are emitted via ``arcagent.core.telemetry`` so every credential
resolution is logged in the tamper-evident audit trail (NIST AU-2/AU-9).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from arctrust import AuditEvent, NullSink, emit

from arcagent.modules.vault.backends.file import FileBackend
from arcagent.modules.vault.protocol import VaultBackend, VaultUnreachable

_logger = logging.getLogger("arcagent.modules.vault.resolver")

# Tier literal set — validated at call site to prevent typos surfacing as
# silent personal-tier fallback in a federal environment.
_VALID_TIERS = frozenset({"federal", "enterprise", "personal"})

# Module-level NullSink — default for callers that don't inject an audit sink.
_NULL_SINK = NullSink()


async def resolve_secret(
    name: str,
    *,
    tier: str,
    backend: VaultBackend | None,
    env_fallback_var: str | None = None,
    audit_sink: Any | None = None,
) -> str:
    """Resolve a named secret according to tier policy.

    Args:
        name: Canonical secret name (used as both vault path and file-backend
            filename).  Must be a non-empty string; no path separators.
        tier: Deployment tier — ``"federal"``, ``"enterprise"``, or
            ``"personal"``.
        backend: The vault backend to try first.  Pass ``None`` if no vault
            is configured (only valid at personal tier).
        env_fallback_var: Name of an environment variable to use as fallback
            (enterprise and personal tiers only).  If ``None`` no env fallback
            is attempted.
        audit_sink: An arctrust AuditSink for vault.unreachable events.
            Defaults to NullSink so existing call sites need no changes.

    Returns:
        Secret value as a plain string.

    Raises:
        ValueError: If ``tier`` is not one of the three valid values.
        RuntimeError: When the secret cannot be resolved according to tier
            policy.
        VaultUnreachable: Propagated at federal tier when the backend is
            unreachable.
    """
    if not name:
        raise ValueError("Secret name must not be empty")
    if tier not in _VALID_TIERS:
        raise ValueError(
            f"Invalid tier {tier!r}. Must be one of: {sorted(_VALID_TIERS)}"
        )

    sink = audit_sink if audit_sink is not None else _NULL_SINK

    if tier == "federal":
        return await _resolve_federal(name, backend=backend, audit_sink=sink)
    if tier == "enterprise":
        return await _resolve_enterprise(
            name, backend=backend, env_var=env_fallback_var, audit_sink=sink
        )
    # personal
    return await _resolve_personal(
        name, backend=backend, env_var=env_fallback_var, audit_sink=sink
    )


# ---------------------------------------------------------------------------
# Federal tier
# ---------------------------------------------------------------------------


async def _resolve_federal(
    name: str,
    *,
    backend: VaultBackend | None,
    audit_sink: Any,
) -> str:
    """Federal: vault is mandatory.  Any failure is a hard error.

    No environment-variable or file fallback is attempted, even if the caller
    set env_fallback_var.  This is intentional: federal environments must not
    allow secrets to arrive via environment injection.

    NIST AU-2/AU-9: vault.unreachable is logged before propagating so the
    audit trail captures the failure even if the caller swallows the exception.
    AuditEvent is also emitted through the arctrust sink (canonical path).
    """
    if backend is None:
        # Defense-in-depth: log AND emit canonical AuditEvent before raising.
        _logger.warning(
            "vault.unreachable secret=%r tier=federal reason=no_backend_configured",
            name,
        )
        _emit_unreachable_audit(name, "federal", "no_backend_configured", audit_sink)
        raise VaultUnreachable(
            f"[federal] No vault backend configured for secret {name!r}. "
            "A vault backend is required at federal tier."
        )

    try:
        value = await backend.get_secret(name)
    except VaultUnreachable:
        _logger.warning(
            "vault.unreachable secret=%r tier=federal reason=backend_raised",
            name,
        )
        _emit_unreachable_audit(name, "federal", "backend_raised", audit_sink)
        raise

    if value is None:
        raise RuntimeError(
            f"[federal] Secret {name!r} not found in vault. "
            "Federal tier requires secrets to exist in the vault."
        )
    return value


# ---------------------------------------------------------------------------
# Enterprise tier
# ---------------------------------------------------------------------------


async def _resolve_enterprise(
    name: str,
    *,
    backend: VaultBackend | None,
    env_var: str | None,
    audit_sink: Any,
) -> str:
    """Enterprise: try vault; on failure warn + audit + try env var."""
    if backend is not None:
        try:
            value = await backend.get_secret(name)
            if value is not None:
                return value
            # Secret not found in vault — treat same as unreachable for
            # fallback purposes; log a warning.
            _logger.warning(
                "[enterprise] Secret %r not found in vault; trying env fallback",
                name,
            )
            _emit_vault_fallback_audit(name, reason="secret_not_found")
        except VaultUnreachable as exc:
            _logger.warning(
                "vault.unreachable secret=%r tier=enterprise reason=%s",
                name,
                exc,
            )
            # Canonical AuditEvent via arctrust (defense-in-depth alongside log).
            _emit_unreachable_audit(name, "enterprise", str(exc), audit_sink)
            _emit_vault_fallback_audit(name, reason=str(exc))

    # Env fallback
    if env_var is not None:
        env_value = os.environ.get(env_var)
        if env_value:
            return env_value

    raise RuntimeError(
        f"[enterprise] Secret {name!r} could not be resolved: "
        f"vault unavailable/missing and env var "
        f"{env_var!r} is not set."
    )


# ---------------------------------------------------------------------------
# Personal tier
# ---------------------------------------------------------------------------


async def _resolve_personal(
    name: str,
    *,
    backend: VaultBackend | None,
    env_var: str | None,
    audit_sink: Any,
) -> str:
    """Personal: vault → env var → ~/.arc/secrets/{name} (0600 enforced)."""
    # 1. Try vault if configured
    if backend is not None:
        try:
            value = await backend.get_secret(name)
            if value is not None:
                return value
        except VaultUnreachable as exc:
            _logger.warning(
                "vault.unreachable secret=%r tier=personal reason=%s",
                name,
                exc,
            )
            # Canonical AuditEvent via arctrust (defense-in-depth alongside log).
            _emit_unreachable_audit(name, "personal", str(exc), audit_sink)

    # 2. Try env var
    if env_var is not None:
        env_value = os.environ.get(env_var)
        if env_value:
            return env_value

    # 3. Try file backend (~/.arc/secrets/{name}, 0600 enforced)
    file_backend = FileBackend()
    file_value = await file_backend.get_secret(name)
    if file_value is not None:
        return file_value

    raise RuntimeError(
        f"[personal] Secret {name!r} could not be resolved: "
        "vault not configured/available, env var not set, and "
        f"~/.arc/secrets/{name} does not exist or has wrong permissions."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit_vault_fallback_audit(name: str, reason: str) -> None:
    """Emit an audit event for vault fallback.

    Structured logging at WARNING level keeps the event visible in all log
    aggregators as defense-in-depth alongside the canonical AuditEvent path.
    """
    _logger.warning(
        "AUDIT vault.fallback secret=%r reason=%r",
        name,
        reason,
    )


def _emit_unreachable_audit(
    name: str,
    tier: str,
    reason: str,
    audit_sink: Any,
) -> None:
    """Emit a canonical vault.unreachable AuditEvent through arctrust.

    Called before raising VaultUnreachable so the audit trail captures the
    failure even when callers catch and suppress the exception (NIST AU-9).

    The logger.warning defense-in-depth line must be emitted by the caller
    before calling this helper — both channels fire independently.
    """
    event = AuditEvent(
        # vault resolver has no agent DID in scope; use sentinel so
        # log aggregators can identify orphaned vault calls.
        actor_did="did:arc:vault-resolver",
        action="vault.unreachable",
        target=name,
        outcome="error",
        tier=tier,
        extra={"reason": reason},
    )
    emit(event, audit_sink)


__all__ = ["resolve_secret"]
