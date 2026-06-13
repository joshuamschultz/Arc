"""Operator helper to install official adapter extension packages.

``arc gateway adapter install telegram`` (and the standalone
``arcgateway adapter install telegram``) call into here. The command is built
from the :data:`OFFICIAL_ADAPTERS` allowlist — a name maps to a fixed
distribution package — so no user-controlled string ever reaches the installer.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from arcgateway.adapters.registry import OFFICIAL_ADAPTERS, discover_plugins, validate_adapter_name


class UnknownAdapterError(KeyError):
    """Raised when an adapter name is not an official extension package."""


class _Completed(Protocol):
    returncode: int


Runner = Callable[[Sequence[str]], Any]


def available_adapters() -> dict[str, str]:
    """Return ``{adapter_name: distribution_package}`` for all official adapters."""
    return dict(OFFICIAL_ADAPTERS)


def installed_adapters() -> set[str]:
    """Return the names of adapter plugins currently discoverable (installed)."""
    return set(discover_plugins())


def build_install_command(
    name: str,
    *,
    upgrade: bool = False,
    prefer_uv: bool | None = None,
) -> list[str]:
    """Build the install command for an official adapter package.

    Args:
        name: Official adapter name (telegram | slack | mattermost).
        upgrade: Pass ``--upgrade`` to reinstall the latest version.
        prefer_uv: Force the uv (True) or pip (False) front-end. ``None``
            auto-detects: uv if it's on PATH, otherwise pip.

    Returns:
        The argv list (never run through a shell).

    Raises:
        ValueError: If ``name`` is not a valid adapter name.
        UnknownAdapterError: If ``name`` is valid but not an official adapter.
    """
    validate_adapter_name(name)
    dist = OFFICIAL_ADAPTERS.get(name)
    if dist is None:
        msg = f"{name!r} is not an official adapter; choose one of {sorted(OFFICIAL_ADAPTERS)}"
        raise UnknownAdapterError(msg)

    use_uv = shutil.which("uv") is not None if prefer_uv is None else prefer_uv
    cmd = ["uv", "pip", "install"] if use_uv else [sys.executable, "-m", "pip", "install"]
    if upgrade:
        cmd.append("--upgrade")
    cmd.append(dist)
    return cmd


def install_adapter(
    name: str,
    *,
    upgrade: bool = False,
    prefer_uv: bool | None = None,
    runner: Runner | None = None,
) -> int:
    """Install an official adapter package and return the installer's exit code.

    Args:
        name: Official adapter name.
        upgrade: Reinstall the latest version.
        prefer_uv: Force uv/pip; ``None`` auto-detects.
        runner: Injectable command runner (defaults to ``subprocess.run``). The
            argv is a fixed allowlist value — no shell, no user-controlled binary.

    Returns:
        The installer process exit code (0 on success).
    """
    cmd = build_install_command(name, upgrade=upgrade, prefer_uv=prefer_uv)
    run = runner if runner is not None else subprocess.run
    proc: _Completed = run(cmd)
    return int(proc.returncode)


__all__ = [
    "UnknownAdapterError",
    "available_adapters",
    "build_install_command",
    "install_adapter",
    "installed_adapters",
]
