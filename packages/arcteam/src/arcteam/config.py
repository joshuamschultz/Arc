"""ArcTeam messaging configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from arcteam.types import MAX_BODY_BYTES


class TeamConfig(BaseModel):
    """ArcTeam configuration with sensible defaults."""

    root: Path = Path.home() / ".arc" / "team"
    hmac_key_env: str = "ARCTEAM_HMAC_KEY"
    max_body_bytes: int = MAX_BODY_BYTES
    default_poll_limit: int = 10
