"""Shared configuration + data-dir resolution for arcstore.

This module is the single source of the Arc data directory and the single
``[arcstore]`` config schema used by every entry point (arcllm, arcrun, arccli,
arcagent). Resolving the same path everywhere is what makes the "call a direct
arcllm now, spin up arcstore later, see the call" guarantee hold — divergent
paths would silently fragment history (SPEC-026 D-013).

``ArcStoreConfig`` is referenced, never redefined, by the other packages
(AC-7.3): one schema, one resolver, one precedence rule.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

ENV_DATA_DIR = "ARCSTORE_DATA_DIR"
"""Environment override for the Arc data directory (highest precedence)."""

_DEFAULT_SUBPATH = (".arc", "store")


def resolve_data_dir(configured: str | Path | None = None) -> Path:
    """Resolve the Arc data directory with a single, shared precedence rule.

    Precedence (SPEC-026 §13.2): ``ARCSTORE_DATA_DIR`` env  >  configured
    ``[arcstore].data_dir``  >  ``~/.arc/store`` default. Every entry point
    calls this same function so a direct ``arc llm`` call and a later
    ``arc agent serve`` agree on the spool/store path.
    """
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        return Path(env).expanduser()
    if configured:
        return Path(configured).expanduser()
    return Path.home().joinpath(*_DEFAULT_SUBPATH)


def store_db_path(data_dir: str | Path | None = None) -> Path:
    """Canonical path to the shared operational store DB (``store/arcui.db``).

    The ``store/arcui.db`` literal was hardcoded in arcagent, arcui, and arccli
    (ARCH-2); one locator over :func:`resolve_data_dir` keeps every entry point
    pointed at the same file — the agent that writes tasks and the dashboard
    that reads them must never diverge on the path.
    """
    return resolve_data_dir(data_dir) / "store" / "arcui.db"


class ArcStoreConfig(BaseModel):
    """The one canonical ``[arcstore]`` block (SPEC-026 FR-7, §13.1).

    arcllm / arcrun / arccli / arcagent reference this model and
    ``resolve_data_dir`` rather than redefining the block, so every entry point
    agrees on the spool/store path and the on/off switch. ``enabled`` is the
    single gate producers and the agent lifecycle check before recording or
    spinning up ingest.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    data_dir: str = ""
    backend: str = "sqlite"
    store_raw_bodies: bool = False
    rotation: str = "daily"
    retention: str = ""
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)

    def resolve_data_dir(self) -> Path:
        """Resolve this config's data dir with the shared env > toml > default rule."""
        return resolve_data_dir(self.data_dir or None)
