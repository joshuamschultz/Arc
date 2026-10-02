"""The deployment's connection-health probe loop and operator-notice delivery (P18-1).

One loop per arcui process, started in the lifespan next to the approval
dispatcher. Each tick does two things:

1. **Probe what is due.** Claim the connections whose ``next_check_at`` has passed
   (a compare-and-set on the record, so two arcui processes or a restart never probe
   one twice in an interval), and run each one's declared ``[health]`` probe through
   :meth:`arcagent.Connections.check_health`. Healthy and unknown connections are
   looked at every 30 minutes, ``error`` every 10, ``needs_you`` every 5, each with
   jitter, so a fleet restart does not synchronise every provider call.
2. **Deliver what is owed.** Claim each connection's due operator notice (a durable
   lease on the record), hand its text to the first granted agent that can reach the
   operator, and finish it. The text is built by the authority; this module only
   carries it.

The loop is a detached causal root (``connector_probe``), so a probe is never
attributed to the page view that happened to be in flight when it ran.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import arcagent
from arctrust import causal
from arctrust.audit import NullSink

from arcui.identity import resolve_agent_did

logger = logging.getLogger("arcui.connection_health")

#: Seconds between ticks. Short, because the work per tick is "claim what is due",
#: and what is due is decided by each record's own ``next_check_at``.
DEFAULT_TICK_SECONDS = 60.0
#: Let the gateway and the embedded agents finish starting before the first probe:
#: a notice produced by the very first tick must have an agent to deliver it.
DEFAULT_INITIAL_DELAY_SECONDS = 5.0

ConnectionsFactory = Callable[[], arcagent.Connections]
AgentsResolver = Callable[[str], Sequence[Any]]
FallbackAgents = Callable[[], Sequence[Any]]


class ConnectionHealthMonitor:
    """Probe due connections and deliver due notices, on a detached timer."""

    def __init__(
        self,
        connections_factory: ConnectionsFactory,
        *,
        store_opener: Callable[[], Awaitable[Any]],
        agents_resolver: AgentsResolver,
        fallback_agents: FallbackAgents = lambda: [],
        sink: Any | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        rng: Callable[[], float] = random.random,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS,
        probe_concurrency: int = 4,
        ui_base: str = "",
    ) -> None:
        self._connections_factory = connections_factory
        self._store_opener = store_opener
        self._agents_resolver = agents_resolver
        self._fallback_agents = fallback_agents
        self._sink = sink
        self._clock = clock
        self._rng = rng
        self._tick_seconds = tick_seconds
        self._initial_delay = initial_delay_seconds
        self._probe_slots = asyncio.Semaphore(probe_concurrency)
        self._ui_base = ui_base
        # Names this process's claim on a notice; a restart is a new owner, which is
        # exactly why a dead owner's lease must expire rather than be inherited.
        self._owner = f"arcui:{uuid.uuid4().hex}"
        self._task: asyncio.Task[None] | None = None
        self._authority: arcagent.ConnectionHealthAuthority | None = None

    def start(self) -> None:
        """Run the loop as its own causal root. Idempotent."""
        if self._task is None or self._task.done():
            self._task = causal.spawn_detached(
                self._run(),
                initiator="connector_probe",
                initiator_id=arcagent.PROBE_DID,
                name="arcui:connection-health",
            )

    async def stop(self) -> None:
        """Cancel the loop and wait for it to unwind."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def tick(self) -> None:
        """One pass: probe what is due, then deliver what is owed."""
        authority = await self._authority_for()
        connections = self._connections_factory()
        now = self._clock()
        try:
            await connections.ensure_health_records()
        except Exception:  # reason: one unreadable config must not stop the probes
            logger.exception("connection health: could not seed records")
        due = await authority.claim_due_checks(now)
        await asyncio.gather(*(self._check(connections, authority, r.connection) for r in due))
        await authority.dispatch_notices(
            self._deliver, owner=self._owner, now=self._clock(), ui_base=self._ui_base
        )

    async def _run(self) -> None:
        await asyncio.sleep(self._initial_delay)
        while True:
            try:
                await self.tick()
            except Exception:  # reason: the loop must outlive any one bad tick
                logger.exception("connection health: tick failed")
            await asyncio.sleep(self._tick_seconds)

    async def _check(
        self,
        connections: arcagent.Connections,
        authority: arcagent.ConnectionHealthAuthority,
        instance: str,
    ) -> None:
        async with self._probe_slots:
            try:
                await connections.check_health(instance, checked_by=arcagent.PROBE_DID)
            except arcagent.ExtensionError as exc:
                # A record whose connection was removed from config: nothing to probe.
                logger.info("connection health: %s not probed: %s", instance, exc.message)
            except Exception:  # reason: one connection's failure is that connection's alone
                logger.exception("connection health: probe of %s failed", instance)
            finally:
                await authority.reschedule(instance, self._clock())

    async def _deliver(self, pending: arcagent.PendingNotice, text: str) -> str | None:
        """Hand the notice to the first agent that can reach the operator.

        Granted agents go first; when none of them can, ANY embedded agent may
        carry it, because the notice is addressed to the deployment's operator,
        not to the connection's grantees. ``None`` when nobody could.
        """
        granted = list(self._agents_resolver(pending.connection))
        others = [a for a in self._fallback_agents() if not any(a is g for g in granted)]
        for agent in (*granted, *others):
            try:
                channel = await agent.notify_operator(
                    text, idempotency_key=pending.idempotency_key
                )
            except Exception:  # reason: one agent's failure must not hide the next one's channel
                logger.warning("connection health: notify_operator failed", exc_info=True)
                continue
            if channel:
                return str(channel)
        return None

    async def _authority_for(self) -> arcagent.ConnectionHealthAuthority:
        if self._authority is None:
            backend = await self._store_opener()
            self._authority = arcagent.ConnectionHealthAuthority(
                arcagent.ConnectionStateStore(backend), sink=self._sink, rng=self._rng
            )
        return self._authority


def granted_embedded_agents(
    app: Any, connections: arcagent.Connections, instance: str
) -> list[Any]:
    """The live, embedded agents granted ``instance``, in grant order.

    Resolved by DID off the roster exactly as the connector routes do: no
    filesystem reach and no arcagent internal import. An agent that is granted but
    not loaded in this process is simply absent; the monitor then falls back to any
    embedded agent, and only if none is loaded is the notice undeliverable.
    """
    provider = getattr(app.state, "roster_provider", None)
    cache = getattr(app.state, "embedded_agent_cache", None)
    if provider is None or cache is None:
        return []
    try:
        granted = connections.registry.get(instance).agents
    except arcagent.ExtensionError:
        return []
    roster = provider()
    agents: list[Any] = []
    for name in granted:
        did = resolve_agent_did(roster, name)
        live = cache.get(did) if did is not None else None
        if live is not None:
            agents.append(live)
    return agents


def embedded_agents(app: Any) -> list[Any]:
    """Every live embedded agent in this process, whatever it is granted."""
    cache = getattr(app.state, "embedded_agent_cache", None)
    return list(cache.values()) if cache is not None else []


def build_connection_health_monitor(
    app: Any, *, initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS
) -> ConnectionHealthMonitor | None:
    """Compose the monitor for this app, or ``None`` when there is no data plane.

    The connections are bound to the process's already-open WORM sink (a second
    sink would contend for its exclusive lock), and the probe state lives in the
    same arcstore the cards read.
    """
    backend = getattr(app.state, "arcstore_backend", None)
    if backend is None or not hasattr(backend, "update_if"):
        return None
    worm = getattr(app.state, "audit_worm", None)
    sink = worm.sink if worm is not None else NullSink()
    ui_base = str(getattr(app.state, "public_base_url", "") or "")

    async def open_backend() -> Any:
        return backend

    def connections_factory() -> arcagent.Connections:
        return arcagent.Connections.for_deployment(
            audit=arcagent.AuditChain.held(sink),
            state_opener=open_backend,
        )

    def agents_resolver(instance: str) -> list[Any]:
        return granted_embedded_agents(app, connections_factory(), instance)

    return ConnectionHealthMonitor(
        connections_factory,
        store_opener=open_backend,
        agents_resolver=agents_resolver,
        fallback_agents=lambda: embedded_agents(app),
        sink=sink,
        initial_delay_seconds=initial_delay_seconds,
        ui_base=ui_base,
    )


__all__ = [
    "ConnectionHealthMonitor",
    "build_connection_health_monitor",
    "embedded_agents",
    "granted_embedded_agents",
]
