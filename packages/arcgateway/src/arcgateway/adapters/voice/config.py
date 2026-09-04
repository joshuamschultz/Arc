"""Voice platform config (SPEC-077 COMP-001).

The ``[platforms.voice]`` block. Kept deliberately small in the Phase 1
skeleton: the WebRTC endpoint, pairing and engine-selection fields land with
Phase 2/3. ``agent_did`` follows the same per-platform override rule every
adapter shares (see ``AdapterBuildContext.agent_did``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class VoicePlatformConfig(BaseModel):
    """Validated shape of the ``[platforms.voice]`` TOML block."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    agent_did: str | None = None


__all__ = ["VoicePlatformConfig"]
