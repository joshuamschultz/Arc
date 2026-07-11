"""Always-on fleet registry — one shared ArcAgent instance per team agent (MSG4).

The embedded gateway (SPEC-023) creates an ``ArcAgent`` lazily, per web-chat
turn, through ``bootstrap._make_agent_factory``. An agent that is never
web-chatted is therefore never started — and its messaging inbox loop (the
durable PUSH consumer that makes it RESPOND to a DM / @mention / channel post)
never runs. ``arc ui start --team-root`` closes that gap by starting every team
agent up front and registering each instance here.

This registry is the one bridge between two composition roots that both live in
the ``arc ui start`` process but cannot otherwise share references: the always-on
fleet (started by arccli, in the app lifespan) and the embedded gateway's agent
factory (built by ``arcgateway.bootstrap`` inside arcui's lifespan, which only
closes over ``team_root``). The factory consults :func:`current_fleet` and
returns the already-started instance instead of constructing a second one — so
there is exactly ONE ArcAgent, hence one durable NATS consumer, per agent,
shared by the inbox loop and by web chat. Without this, a web-chatted agent
would spin up a second instance whose duplicate durable consumer competes with
the always-on one for the same stream.

Process-scoped by design: one ``arc ui start`` process serves one fleet. It is
set once at startup and cleared on teardown (and by tests). Not thread-safe —
arc runs a single asyncio event loop per process.
"""

from __future__ import annotations

from typing import Any


class FleetRegistry:
    """DID-keyed registry of started, always-on ``ArcAgent`` instances.

    Holds strong references so the started agents (and their running inbox-loop
    tasks) are not garbage-collected for the process lifetime.
    """

    def __init__(self) -> None:
        self._agents: dict[str, Any] = {}

    def add(self, agent_did: str, agent: Any) -> None:
        """Register a started agent under its DID (last-wins on a re-register)."""
        self._agents[agent_did] = agent

    def get(self, agent_did: str) -> Any | None:
        """Return the started agent for ``agent_did``, or ``None`` if not in the fleet."""
        return self._agents.get(agent_did)

    def dids(self) -> list[str]:
        """Every DID currently in the fleet."""
        return list(self._agents)

    def __len__(self) -> int:
        return len(self._agents)


_current: FleetRegistry | None = None


def set_current_fleet(fleet: FleetRegistry | None) -> None:
    """Install (or clear, with ``None``) the process fleet the factory consults."""
    global _current
    _current = fleet


def current_fleet() -> FleetRegistry | None:
    """Return the process fleet, or ``None`` when no always-on fleet is running."""
    return _current


__all__ = ["FleetRegistry", "current_fleet", "set_current_fleet"]
