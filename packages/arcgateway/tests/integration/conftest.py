"""Integration test conftest — adds arcagent/arcrun/arcllm to sys.path.

Separated from the root conftest so that unit tests (especially the
slack-bolt mock tests) are not affected by arcagent imports being
available at collection time.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGES_DIR = Path(__file__).parent.parent.parent.parent  # packages/

_EXTRA_PATHS = [
    _PACKAGES_DIR / "arcagent" / "src",
    _PACKAGES_DIR / "arcrun" / "src",
    _PACKAGES_DIR / "arcllm" / "src",
]


def pytest_configure(config: object) -> None:
    """Add arcagent/arcrun/arcllm src dirs to sys.path for integration tests."""
    for path in _EXTRA_PATHS:
        if path.exists():
            path_str = str(path)
            if path_str not in sys.path:
                sys.path.insert(0, path_str)
