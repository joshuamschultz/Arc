"""AgentRegistry — in-memory agent connection tracking.

No persistence. Agents re-register on UI restart. Each agent gets a
per-agent RollingAggregator for drill-down stats.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from arcui.types import AgentRegistration

logger = logging.getLogger(__name__)


@dataclass
class AgentEntry:
    """A connected agent with its WebSocket and per-agent aggregator."""

    registration: AgentRegistration
    ws: Any  # starlette.websockets.WebSocket at runtime
    aggregator: Any | None = None  # RollingAggregator, injected by server


class AgentRegistry:
    """In-memory registry of connected agents.

    Simple dict-based storage. Reads (get, list_agents, is_full) are
    lock-free since CPython dict reads are thread-safe. Writes
    (register, unregister) are single-threaded in asyncio context.
    """

    def __init__(self, max_agents: int = 100) -> None:
        self._agents: dict[str, AgentEntry] = {}
        self.max_agents = max_agents

    def register(
        self,
        agent_id: str,
        ws: Any,
        registration: AgentRegistration,
    ) -> AgentEntry:
        """Register a new agent connection.

        Returns the created AgentEntry. Caller should check is_full()
        before calling this to enforce capacity limits.
        """
        entry = AgentEntry(registration=registration, ws=ws)
        self._agents[agent_id] = entry
        logger.info("Agent registered: %s (%s)", agent_id, registration.agent_name)
        return entry

    def unregister(self, agent_id: str) -> None:
        """Remove an agent from the registry."""
        entry = self._agents.pop(agent_id, None)
        if entry:
            logger.info(
                "Agent unregistered: %s (%s)",
                agent_id,
                entry.registration.agent_name,
            )

    def get(self, agent_id: str) -> AgentEntry | None:
        """Look up an agent by ID. Returns None if not found."""
        return self._agents.get(agent_id)

    def list_agents(self) -> list[AgentRegistration]:
        """Return registrations of all connected agents."""
        return [e.registration for e in self._agents.values()]

    def is_full(self) -> bool:
        """Whether the registry has reached max capacity."""
        return len(self._agents) >= self.max_agents
