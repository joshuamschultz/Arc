"""Test configuration for arctrust tests.

Adds the arctrust src directory to sys.path so tests can import the package
when running from either the workspace root or the package directory.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_arctrust_on_path() -> None:
    """Add packages/arctrust/src to sys.path if not already present.

    Works both when pytest is invoked from workspace root and from
    the packages/arctrust/ directory directly.
    """
    # __file__ is packages/arctrust/tests/conftest.py
    # parent is packages/arctrust/tests/
    # parents[1] is packages/arctrust/
    # parents[1] / "src" is packages/arctrust/src/
    src = Path(__file__).parent.parent / "src"
    if src.exists() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


_ensure_arctrust_on_path()
