"""Backend loader with trusted wrapper application.

This module is the SINGLE POINT where backends and wrapper come together:
- Loads a backend (built-in 'direct' or external dotted path)
- Applies Arc's trusted middleware
- Returns the wrapped provider to consumers

If the trusted wrapper relocates:
- Update the wrapper import below (lines marked RELOCATION)
- Backends and contract stay in arcllm
- Consumers (arcrun, arcmemory, arccli, arcui) continue calling load_model()

Backend discovery (modeled on arcrun/backends/loader.py):
- Built-in: 'direct' (current HTTP adapters)
- External: dotted path 'pkg.mod:Class'
- Entry points PERMANENTLY DISABLED (Phase C)
- Signed manifest required for external backends at all tiers
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from arcllm.contract.types import ModelProvider

# RELOCATION: If wrapper moves, update this import
# from arcllm.trusted.wrapper import apply_trusted_middleware

__all__ = [
    "BackendPolicyError",
    "load_model_backend",
]

logger = logging.getLogger(__name__)


class BackendPolicyError(RuntimeError):
    """Entry-points disabled at all tiers (Phase C supply-chain lockdown)."""


# ---------------------------------------------------------------------------
# Audit event emission (placeholder - integrate arctrust)
# ---------------------------------------------------------------------------


def _emit_loaded(name: str, *, tier: str, path: str, sink: Any | None = None) -> None:
    """Emit model.backend.loaded audit event."""
    msg = f"Model backend '{name}' loaded (tier={tier}, path={path})"
    logger.info(msg)
    # TODO: arctrust.audit.emit("model.backend.loaded", {...}, sink=sink)


def _emit_denied(name: str, *, tier: str, reason: str, sink: Any | None = None) -> None:
    """Emit model.backend.denied audit event."""
    msg = f"Model backend '{name}' denied (tier={tier}): {reason}"
    logger.warning(msg)
    # TODO: arctrust.audit.emit("model.backend.denied", {...}, sink=sink)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_model_backend(
    name: str,
    *,
    tier: str = "personal",
    manifest_path: Path | None = None,
    trust_dir: Path | None = None,
    audit_sink: Any | None = None,
    # Wrapper configuration (to be expanded)
    apply_wrapper: bool = True,
    telemetry_config: dict[str, Any] | None = None,
    security_config: dict[str, Any] | None = None,
) -> ModelProvider:
    """Load and wrap a model backend.

    This is the single factory function consumers call. It loads the backend
    and applies Arc's trusted middleware.

    Parameters
    ----------
    name:
        Backend name. Built-in: 'direct'. External: 'pkg.mod:Class'.
    tier:
        Deployment tier: 'federal', 'enterprise', 'personal'.
    manifest_path:
        Signed manifest for external backends (required at all tiers).
    trust_dir:
        Optional issuer trust store override.
    audit_sink:
        Optional arctrust.AuditSink for audit events.
    apply_wrapper:
        Whether to apply Arc's trusted middleware (default True).
    telemetry_config:
        Telemetry middleware config (spool, trace, FIPS).
    security_config:
        Security middleware config (redaction, signing).

    Returns
    -------
    ModelProvider
        Backend wrapped with Arc's trusted middleware (if apply_wrapper=True).

    Raises
    ------
    BackendPolicyError
        Entry-points disabled; external backends need signed manifest.
    ValueError
        Backend not found or doesn't implement ModelProvider.
    """
    # Load the bare backend
    if name == "direct":
        backend = _load_builtin_direct()
        _emit_loaded(name, tier=tier, path="<builtin:direct>", sink=audit_sink)
    else:
        # External backend via dotted path
        if ":" not in name and "." not in name:
            _emit_denied(
                name,
                tier=tier,
                reason="entry_points disabled (Phase C)",
                sink=audit_sink,
            )
            raise BackendPolicyError(
                f"Backend '{name}' not built-in and entry_points disabled (Phase C). "
                "Supply dotted path 'pkg:Class' with signed manifest."
            )

        if manifest_path is None:
            _emit_denied(
                name,
                tier=tier,
                reason="manifest required for external backends (Phase C)",
                sink=audit_sink,
            )
            raise ValueError(
                f"External backend '{name}' requires signed manifest at all tiers (Phase C)."
            )

        # TODO: Verify manifest signature, content hash
        backend = _load_dotted(name)
        _emit_loaded(name, tier=tier, path=name, sink=audit_sink)

    # Apply Arc's trusted middleware
    if not apply_wrapper:
        return backend

    # RELOCATION: If wrapper moves, update this section
    # wrapped = apply_trusted_middleware(
    #     backend,
    #     telemetry=telemetry_config,
    #     security=security_config,
    #     ...
    # )
    # return wrapped

    # Placeholder: return bare backend until wrapper is implemented
    return backend


# ---------------------------------------------------------------------------
# Internal resolvers
# ---------------------------------------------------------------------------


def _load_builtin_direct() -> ModelProvider:
    """Load the built-in 'direct' backend (current HTTP adapters).

    The 'direct' backend is today's arcllm: adapters, router, retry, fallback,
    rate-limit. It becomes one backend among many.
    """
    # TODO: In next commit, restructure current arcllm as backends.direct
    # and return a DirectBackend instance that implements ModelProvider
    raise NotImplementedError("Built-in 'direct' backend not yet restructured")


def _load_dotted(path: str) -> ModelProvider:
    """Import 'pkg.mod:Class' and return an instance."""
    if ":" in path:
        module_path, class_name = path.rsplit(":", 1)
    else:
        module_path, class_name = path.rsplit(".", 1)

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ValueError(f"Cannot import '{module_path}': {exc}") from exc

    cls: Any = getattr(module, class_name, None)
    if cls is None:
        raise ValueError(f"Module '{module_path}' has no '{class_name}'")

    try:
        instance = cls()
    except Exception as exc:
        raise ValueError(f"Cannot instantiate '{class_name}': {exc}") from exc

    if not isinstance(instance, ModelProvider):
        raise ValueError(
            f"'{path}' does not implement ModelProvider. "
            "Required: name, model_name, invoke(), invoke_stream(), validate_config(), close()."
        )
    return instance
