"""Extension sandbox context managers and entry point discovery.

Extracted from arcagent.core.extensions to keep the core LOC budget within
limits (ADR-004 / G1.5). Re-exported from arcagent.core.extensions for API
stability.
"""

from __future__ import annotations

import builtins
import contextlib
import importlib.metadata
import os
import subprocess
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any


@contextlib.contextmanager
def _filesystem_sandbox(boundaries: list[str]) -> Iterator[None]:
    """Restrict filesystem access to paths within boundary directories.

    Patches builtins.open and Path.read_text/read_bytes/write_text/write_bytes
    to only permit access within the specified boundary paths.
    All patches are restored in the finally block.
    """
    original_open = builtins.open
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes
    original_write_text = Path.write_text
    original_write_bytes = Path.write_bytes

    def _check(file: Any) -> None:
        resolved = str(Path(str(file)).resolve())
        if not any(resolved.startswith(b) for b in boundaries):
            raise PermissionError(f"Sandbox: access denied to {resolved}")

    def _restricted_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        _check(file)
        return original_open(file, *args, **kwargs)

    def _restricted_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        _check(self)
        return original_read_text(self, *args, **kwargs)

    def _restricted_read_bytes(self: Path, *args: Any, **kwargs: Any) -> bytes:
        _check(self)
        return original_read_bytes(self, *args, **kwargs)

    def _restricted_write_text(self: Path, *args: Any, **kwargs: Any) -> int:
        _check(self)
        return original_write_text(self, *args, **kwargs)

    def _restricted_write_bytes(self: Path, *args: Any, **kwargs: Any) -> int:
        _check(self)
        return original_write_bytes(self, *args, **kwargs)

    try:
        builtins.open = _restricted_open
        Path.read_text = _restricted_read_text  # type: ignore[method-assign]
        Path.read_bytes = _restricted_read_bytes  # type: ignore[method-assign]
        Path.write_text = _restricted_write_text  # type: ignore[method-assign]
        Path.write_bytes = _restricted_write_bytes  # type: ignore[method-assign]
        yield
    finally:
        builtins.open = original_open
        Path.read_text = original_read_text  # type: ignore[method-assign]
        Path.read_bytes = original_read_bytes  # type: ignore[method-assign]
        Path.write_text = original_write_text  # type: ignore[method-assign]
        Path.write_bytes = original_write_bytes  # type: ignore[method-assign]


@contextlib.contextmanager
def _paths_sandbox(workspace: Path, allowed_paths: list[Path]) -> Iterator[None]:
    """Restrict filesystem access to workspace + allowed_paths."""
    boundaries = [str(workspace.resolve())]
    boundaries.extend(str(p.resolve()) for p in allowed_paths)
    with _filesystem_sandbox(boundaries):
        yield


@contextlib.contextmanager
def _strict_sandbox(workspace: Path) -> Iterator[None]:
    """Restrict filesystem, subprocess, and network access during factory call."""
    original_run = subprocess.run
    original_popen = subprocess.Popen
    original_os_system = os.system
    original_os_popen = os.popen
    original_urlopen = urllib.request.urlopen

    def _blocked_subprocess(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("Strict sandbox: subprocess execution denied")

    class _BlockedPopen:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise PermissionError("Strict sandbox: subprocess execution denied")

    def _blocked_network(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("Strict sandbox: network access denied")

    try:
        subprocess.run = _blocked_subprocess
        subprocess.Popen = _BlockedPopen  # type: ignore[misc,assignment]
        os.system = _blocked_subprocess
        os.popen = _blocked_subprocess
        urllib.request.urlopen = _blocked_network
        with _filesystem_sandbox([str(workspace.resolve())]):
            yield
    finally:
        subprocess.run = original_run
        subprocess.Popen = original_popen  # type: ignore[misc]
        os.system = original_os_system
        os.popen = original_os_popen
        urllib.request.urlopen = original_urlopen


def _discover_entry_points() -> list[Any]:
    """Discover extensions registered via importlib.metadata entry points.

    Returns entry points from the ``arcagent.extensions`` group.
    Isolated as a function to allow test mocking.
    """
    eps = importlib.metadata.entry_points()
    return list(eps.select(group="arcagent.extensions"))
