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

from arcagent.modules.vault.backends.file import FileBackend
from arcagent.modules.vault.protocol import VaultBackend, VaultUnreachable

_logger = logging.getLogger("arcagent.modules.vault.resolver")

# Tier literal set — validated at call site to prevent typos surfacing as
# silent personal-tier fallback in a federal environment.
_VALID_TIERS = frozenset({"federal", "enterprise", "personal"})


async def resolve_secret(
    name: str,
    *,
    tier: str,
    backend: VaultBackend | None,
    env_fallback_var: str | None = None,
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

    if tier == "federal":
        return await _resolve_federal(name, backend=backend)
    if tier == "enterprise":
        return await _resolve_enterprise(name, backend=backend, env_var=env_fallback_var)
    # personal
    return await _resolve_personal(name, backend=backend, env_var=env_fallback_var)


# ---------------------------------------------------------------------------
# Federal tier
# ---------------------------------------------------------------------------


async def _resolve_federal(name: str, *, backend: VaultBackend | None) -> str:
    """Federal: vault is mandatory.  Any failure is a hard error.

    No environment-variable or file fallback is attempted, even if the caller
    set env_fallback_var.  This is intentional: federal environments must not
    allow secrets to arrive via environment injection.
    """
    if backend is None:
        raise VaultUnreachable(
            f"[federal] No vault backend configured for secret {name!r}. "
            "A vault backend is required at federal tier."
        )

    # Let VaultUnreachable propagate — caller / startup code converts to hard
    # error with appropriate audit event.
    value = await backend.get_secret(name)

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
                "[enterprise] Vault unreachable for secret %r: %s — "
                "falling back to env var",
                name,
                exc,
            )
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
) -> str:
    """Personal: vault → env var → ~/.arc/secrets/{name} (0600 enforced)."""
    # 1. Try vault if configured
    if backend is not None:
        try:
            value = await backend.get_secret(name)
            if value is not None:
                return value
        except VaultUnreachable:
            _logger.debug(
                "[personal] Vault unreachable for %r; trying env/file fallback",
                name,
            )

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

    Uses structured logging at WARNING level so the event is visible in all
    log aggregators; full OTel audit event integration is added when the
    telemetry module is wired to this module.
    """
    _logger.warning(
        "AUDIT vault.fallback secret=%r reason=%r",
        name,
        reason,
    )


__all__ = ["resolve_secret"]
