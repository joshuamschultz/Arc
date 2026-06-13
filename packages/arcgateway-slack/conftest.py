"""Root conftest for arcgateway-slack tests — src-layout sys.path bridge."""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGES_DIR = Path(__file__).resolve().parent.parent


def pytest_configure(config: object) -> None:
    _add(Path(__file__).resolve().parent / "src")
    for sibling in ("arcgateway", "arcagent", "arcrun", "arcllm", "arctrust"):
        src = _PACKAGES_DIR / sibling / "src"
        if src.exists():
            _add(src)


def _add(path: Path) -> None:
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
