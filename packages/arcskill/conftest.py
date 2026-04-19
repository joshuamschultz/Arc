"""conftest.py — pytest configuration for arcskill tests.

Adds the src directory to sys.path so that `import arcskill` works without
installing the package first.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make arcskill importable from the source tree.
_src = Path(__file__).parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))
