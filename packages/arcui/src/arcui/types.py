"""Multi-agent UI agent-registration types.

Flat Pydantic model for the identity + capabilities an agent reports on connect.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_MAX_TOOLS_COUNT = 100
_MAX_MODULES_COUNT = 100


class AgentRegistration(BaseModel):
    """Identity and capabilities sent by an agent on connect."""

    agent_id: str
    agent_name: str = Field(max_length=256)
    model: str
    provider: str
    team: str | None = None
    tools: list[str] = Field(default_factory=list, max_length=_MAX_TOOLS_COUNT)
    modules: list[str] = Field(default_factory=list, max_length=_MAX_MODULES_COUNT)
    workspace: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    connected_at: str
    last_event_at: str | None = None
    sequence: int = 0
