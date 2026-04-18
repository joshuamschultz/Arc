"""Root conftest.py for arcgateway tests.

Sets up sys.path so that tests can:
1. Import arcgateway (from src/ layout) even without an installed wheel.
2. Spawn arc-agent-worker subprocesses that find arccli.agent_worker.
3. Import arcagent, arcrun, arcllm for E2E tests that need them.

The editable install via uv does NOT process .pth files correctly when
the Python interpreter version (cpython-3.12.13) does not match the
lib-dynload symlink (cpython-3.12). This conftest bridges that gap.

Important:
  arcagent/src is added to sys.path lazily (via a pytest fixture, not at
  configure time) to avoid interfering with the slack-bolt import mock tests.
  Those tests rely on slack_bolt NOT being pre-imported by arcagent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_PACKAGES_DIR = Path(__file__).parent.parent


def pytest_configure(config: object) -> None:
    """Add arcgateway/src and arccli/src to sys.path.

    These are the minimum paths needed for all tests:
    - arcgateway/src: the package under test
    - arccli/src: needed for subprocess executor tests (arc-agent-worker)

    arcagent/src and arcrun/src are added in a session fixture below
    (lazily) to avoid interfering with slack-bolt mock tests.
    """
    _add_to_path(Path(__file__).parent / "src")

    arccli_src = _PACKAGES_DIR / "arccli" / "src"
    if arccli_src.exists():
        _add_to_path(arccli_src)
        # Also propagate to subprocess workers (SubprocessExecutor tests)
        _add_to_pythonpath(str(arccli_src))


def _add_to_path(path: Path) -> None:
    """Add path to sys.path if not already present."""
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


def _add_to_pythonpath(path_str: str) -> None:
    """Add path to PYTHONPATH environment variable for subprocess workers."""
    existing = os.environ.get("PYTHONPATH", "")
    if path_str not in existing:
        new = path_str + (os.pathsep + existing if existing else "")
        os.environ["PYTHONPATH"] = new
