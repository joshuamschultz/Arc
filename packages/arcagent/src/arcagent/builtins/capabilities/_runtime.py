"""Per-agent runtime context for built-in capabilities.

The ``@tool`` decorator stamps a plain async function — there's no
constructor where the tool can capture state. So workspace path,
allowed read paths, vault resolver, and the agent's
:class:`CapabilityLoader` instance live here as module-level state,
configured once by the agent at startup.

Setting these is *not* a global event bus or shared singleton across
multiple agents; one agent process owns one set of values. If two
agents ever shared one process they would step on each other — but
the existing arcagent runtime model is single-agent-per-process, so
this matches.

Tools call :func:`workspace` / :func:`allowed_paths` / :func:`loader`
/ :func:`get_secret` lazily at execute time. If unset, they raise
:class:`RuntimeError` with a clear message rather than silently
falling back — a misconfigured agent must fail loudly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcagent.core.capability_loader import CapabilityLoader

_workspace: Path | None = None
_allowed_paths: list[Path] | None = None
_loader: CapabilityLoader | None = None
_vault_resolver: Any = None


def configure(
    *,
    workspace: Path,
    allowed_paths: list[Path] | None = None,
    loader: CapabilityLoader | None = None,
    vault_resolver: Any = None,
) -> None:
    """Bind per-agent runtime state. Called once at agent startup.

    Subsequent calls overwrite — used by tests to reset between
    runs. Production agents should call exactly once during startup.
    """
    global _workspace, _allowed_paths, _loader, _vault_resolver
    _workspace = workspace.resolve()
    _allowed_paths = allowed_paths
    _loader = loader
    _vault_resolver = vault_resolver


def workspace() -> Path:
    """Return the current agent's workspace root.

    Raises ``RuntimeError`` if :func:`configure` has not been called.
    """
    if _workspace is None:
        raise RuntimeError(
            "builtin tool called before runtime is configured; "
            "agent must call _runtime.configure(workspace=...) at startup"
        )
    return _workspace


def allowed_paths() -> list[Path] | None:
    """Return the list of additional readable paths (e.g. memory dirs)."""
    return _allowed_paths


def loader() -> CapabilityLoader:
    """Return the agent's :class:`CapabilityLoader`.

    Required by ``reload``, ``create_tool``, etc. Raises if unset.
    """
    if _loader is None:
        raise RuntimeError("self-modification tool called before loader is configured")
    return _loader


def get_secret(name: str) -> str | None:
    """Resolve a secret by name. Vault first, env var fallback.

    Mirrors the legacy ``ExtensionAPI.get_secret`` resolution order so
    migrated extensions keep working unchanged:

      1. Vault backend (if configured in [vault] of arcagent.toml)
      2. Environment variable (name uppercased, hyphens → underscores)

    Returns ``None`` if neither path resolves.
    """
    if _vault_resolver is not None:
        try:
            raw_val = _vault_resolver.get_secret(name)
        except Exception:
            raw_val = None
        if raw_val:
            return str(raw_val)
    env_name = name.upper().replace("-", "_")
    return os.environ.get(env_name)


def reset() -> None:
    """Clear all runtime state. Test-only helper."""
    global _workspace, _allowed_paths, _loader, _vault_resolver
    _workspace = None
    _allowed_paths = None
    _loader = None
    _vault_resolver = None


__all__ = [
    "allowed_paths",
    "configure",
    "get_secret",
    "loader",
    "reset",
    "workspace",
]
