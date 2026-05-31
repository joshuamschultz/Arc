"""Shared configuration + data-dir resolution for arcstore.

This module is the single source of the Arc data directory used by every entry
point (arcllm, arcrun, arccli). Resolving the same path everywhere is what makes
the "call a direct arcllm now, spin up arcstore later, see the call" guarantee
hold — divergent paths would silently fragment history (SPEC-026 D-013).

Phase 1 provides the resolver. The full ``ArcStoreConfig`` TOML model
(``enabled``, ``backend``, ``store_raw_bodies``, ...) lands in Phase 5.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_DATA_DIR = "ARCSTORE_DATA_DIR"
"""Environment override for the Arc data directory (highest precedence)."""

_DEFAULT_SUBPATH = (".arc", "store")


def resolve_data_dir() -> Path:
    """Resolve the Arc data directory.

    Precedence (Phase 5 inserts the TOML layer between env and default):
        ``ARCSTORE_DATA_DIR`` env  >  ``~/.arc/store`` default.
    """
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        return Path(env).expanduser()
    return Path.home().joinpath(*_DEFAULT_SUBPATH)
