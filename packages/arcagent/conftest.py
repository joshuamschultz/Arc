"""conftest.py — workspace path injection for pytest.

When running pytest from the arcagent package directory, other workspace
packages (arcrun, arcllm, arccli) are not automatically on sys.path.
This conftest adds them so tests can import from sibling packages.
"""

import sys
from pathlib import Path

# Workspace root is two levels up from this file
_WORKSPACE_ROOT = Path(__file__).parent.parent.parent

_WORKSPACE_PKGS = [
    "arcrun",
    "arcllm",
    "arccli",
    "arcteam",
    "arcmas",
    "arcgateway",
    "arcmodel",
    "arcprompt",
    "arcskill",
]

for _pkg in _WORKSPACE_PKGS:
    _src = _WORKSPACE_ROOT / "packages" / _pkg / "src"
    if _src.exists() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
