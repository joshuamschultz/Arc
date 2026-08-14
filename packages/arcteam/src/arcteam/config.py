"""ArcTeam messaging configuration."""

from __future__ import annotations

import os
from pathlib import Path

from arctrust.paths import arc_team, nats_dir
from pydantic import BaseModel, Field

from arcteam.types import MAX_BODY_BYTES


def default_team_root() -> Path:
    """Resolve the team data root — :func:`arctrust.paths.arc_team`."""
    return arc_team()


def default_nats_url() -> str:
    """Resolve the team broker url: ``$ARCTEAM_NATS_URL`` or the local default.

    Single source of truth for every surface that starts, connects to, or
    reports on the broker. A surface that ensures a broker at one url while
    another connects to a second produces the exact failure this resolver
    exists to prevent: two healthy-looking halves on separate buses, and an
    inbox that reads empty because nothing it can see is there.
    """
    return os.environ.get("ARCTEAM_NATS_URL", "nats://127.0.0.1:4222")


def default_jetstream_store_dir() -> Path:
    """Resolve the NATS JetStream store: ``${ARC_CONFIG_DIR:-~/.arc}/nats/jetstream``.

    Channel/message/entity/team registry data lives here for the live CLI +
    dashboard path, so it must honor ``ARC_CONFIG_DIR`` alongside the team root
    to keep a self-contained deployment folder self-contained.
    """
    return nats_dir() / "jetstream"


class TeamConfig(BaseModel):
    """ArcTeam configuration with sensible defaults."""

    root: Path = Field(default_factory=default_team_root)
    max_body_bytes: int = MAX_BODY_BYTES
    default_poll_limit: int = 10
