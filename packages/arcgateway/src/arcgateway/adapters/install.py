"""Operator helper to install a platform's optional dependencies.

``arc gateway adapter install telegram`` (and the standalone ``arcgateway
adapter install telegram``) call into here.

Since SPEC-065 a platform *is* a folder in this package — there is nothing to
install to make it discoverable. What can be missing is the third-party client
it needs (``python-telegram-bot``, ``slack-bolt``, ``aiohttp``), which ships as
an extra of this distribution. So this module installs ``arcgateway[<name>]``.

The requirement string is built from the platform's own :attr:`AdapterSpec.name`
after :func:`validate_adapter_name`, so no user-controlled string ever reaches
the installer and nothing runs through a shell.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from importlib.metadata import PackageNotFoundError, distribution
from typing import Any, Protocol

from arcgateway.adapters.registry import discover_adapters, validate_adapter_name


class UnknownAdapterError(KeyError):
    """Raised when an adapter name is not a platform folder in this package."""


class _Completed(Protocol):
    returncode: int


Runner = Callable[[Sequence[str]], Any]


def available_adapters() -> dict[str, str]:
    """Return ``{platform_name: pip requirement that enables it}``.

    Derived from the discovered roster, so a platform folder added to the tree
    appears here with no edit — the same property the registry has.
    """
    return {spec.name: f"arcgateway[{spec.name}]" for spec in discover_adapters()}


def installed_adapters() -> set[str]:
    """Return the platforms whose declared requirements are all present.

    A folder is always discoverable; what makes it *usable* is its client
    library. Reporting discoverability here would tell an operator a platform
    is ready when connecting to it would still fail.
    """
    ready: set[str] = set()
    for spec in discover_adapters():
        if all(_is_installed(requirement) for requirement in spec.requires):
            ready.add(spec.name)
    return ready


def _is_installed(distribution_name: str) -> bool:
    try:
        distribution(distribution_name)
    except PackageNotFoundError:
        return False
    return True


def build_install_command(
    name: str,
    *,
    upgrade: bool = False,
    prefer_uv: bool | None = None,
) -> list[str]:
    """Build the install command for a platform's extra.

    Args:
        name: Platform name (telegram | slack | mattermost | …).
        upgrade: Pass ``--upgrade`` to reinstall the latest version.
        prefer_uv: Force the uv (True) or pip (False) front-end. ``None``
            auto-detects: uv if it's on PATH, otherwise pip.

    Returns:
        The argv list (never run through a shell).

    Raises:
        ValueError: If ``name`` is not a valid adapter name.
        UnknownAdapterError: If ``name`` is valid but there is no such platform.
    """
    validate_adapter_name(name)
    requirement = available_adapters().get(name)
    if requirement is None:
        known = sorted(available_adapters())
        msg = f"{name!r} is not a platform in this gateway; choose one of {known}"
        raise UnknownAdapterError(msg)

    use_uv = shutil.which("uv") is not None if prefer_uv is None else prefer_uv
    cmd = ["uv", "pip", "install"] if use_uv else [sys.executable, "-m", "pip", "install"]
    if upgrade:
        cmd.append("--upgrade")
    cmd.append(requirement)
    return cmd


def install_adapter(
    name: str,
    *,
    upgrade: bool = False,
    prefer_uv: bool | None = None,
    runner: Runner | None = None,
) -> int:
    """Install a platform's optional dependencies; return the installer's exit code.

    Args:
        name: Platform name.
        upgrade: Reinstall the latest version.
        prefer_uv: Force uv/pip; ``None`` auto-detects.
        runner: Injectable command runner (defaults to ``subprocess.run``). The
            argv is built from a validated platform name — no shell, no
            user-controlled binary.

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
