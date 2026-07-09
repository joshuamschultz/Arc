"""ArcTeam messaging configuration."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

from arcteam.types import MAX_BODY_BYTES


def default_team_root() -> Path:
    """Resolve the team data root: ``${ARC_CONFIG_DIR:-~/.arc}/team``.

    Mirrors the config-dir resolution used across Arc (arcagent config, arc
    identity) so an isolated ``ARC_CONFIG_DIR`` keeps team/channel/entity data
    local instead of leaking to the global ``~/.arc``.
    """
    base = os.environ.get("ARC_CONFIG_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".arc"
    return root / "team"


class TeamConfig(BaseModel):
    """ArcTeam configuration with sensible defaults."""

    root: Path = Field(default_factory=default_team_root)
    max_body_bytes: int = MAX_BODY_BYTES
    default_poll_limit: int = 10
