"""Top-level test configuration for architecture tests.

Ensures workspace sibling packages (arcllm, etc.) are on sys.path when running
architecture tests from the workspace root via `uv run pytest tests/`.

The architecture test for ExecutorBackend (test_backend_protocol_duck_typing.py)
imports from arcrun.backends, which triggers arcrun.__init__ → arcllm import.
arcllm is a workspace-editable package whose source is in packages/arcllm/src.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_workspace_packages_on_path() -> None:
    """Add workspace package src directories to sys.path if not already present."""
    workspace_root = Path(__file__).parents[1]  # Arc/
    packages_dir = workspace_root / "packages"
    if not packages_dir.exists():
        return
    for pkg_src in packages_dir.glob("*/src"):
        if pkg_src.is_dir() and str(pkg_src) not in sys.path:
            sys.path.insert(0, str(pkg_src))


_ensure_workspace_packages_on_path()
