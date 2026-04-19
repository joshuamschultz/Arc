"""Configure sys.path for backends unit tests.

arcrun.backends imports arcrun.__init__ (parent package), which in turn
imports arcllm.  The uv test runner for the arcrun package alone does not
include arcllm on sys.path.  We add it here so backends tests are self-contained.

This mirrors the approach used by the top-level workspace test runner where all
packages are installed as editable dependencies.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_arcllm_on_path() -> None:
    arcllm_src = Path(__file__).parents[6] / "arcllm" / "src"
    if arcllm_src.exists() and str(arcllm_src) not in sys.path:
        sys.path.insert(0, str(arcllm_src))


_ensure_arcllm_on_path()
