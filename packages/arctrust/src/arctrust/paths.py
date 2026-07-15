"""Arc-home path resolution — the single source of truth for ``~/.arc``.

Every Arc package that needs the user-wide config root (fleet TOML files, the
operator-key custody dir, the approval store) resolves it here so the env
override is honored uniformly. arcagent's config loader, arcui's system-config
and operator surfaces, and the CLI all funnel through these helpers rather than
re-deriving ``Path.home() / ".arc"`` independently.
"""

from __future__ import annotations

import os
from pathlib import Path


def arc_home() -> Path:
    """Return the user-wide Arc config root: ``${ARC_CONFIG_DIR:-~/.arc}``.

    ``ARC_CONFIG_DIR`` overrides the default so tests and alternate deployments
    can relocate the whole config tree without touching call sites.
    """
    base = os.environ.get("ARC_CONFIG_DIR")
    return Path(base).expanduser() if base else Path.home() / ".arc"


def default_operator_key_path() -> Path:
    """Return the on-box operator-key file: ``<arc_home>/operator/operator.key``.

    The operator key is the deployment audit authority (see :mod:`arctrust.operator`);
    it lives outside any agent workspace tool-sandbox. arcui's operator-gated
    surfaces load it read-only from here to sign approvals and record the approver DID.
    """
    return arc_home() / "operator" / "operator.key"


__all__ = ["arc_home", "default_operator_key_path"]
