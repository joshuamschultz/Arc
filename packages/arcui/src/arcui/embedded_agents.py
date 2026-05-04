"""Wire chat-loaded agents into the existing arcui telemetry surface.

The embedded gateway (SPEC-023) loads an ``ArcAgent`` on demand inside the
arcui process whenever a browser hits ``/ws/chat/{agent_id}``. Without
this module, those agents would never appear in the ``Agent Fleet``
LIVE counter (which tracks the WebSocket-based agent_registry) — even
though they are running, in-process, and answering messages.

We resolve that by intercepting the executor's ``agent_factory`` once
at lifespan startup. The wrapper:

  1. Caches the loaded agent by ``agent_did`` in a bounded LRU so each
     turn reuses the same instance — no repeated ``startup()`` cost per
     chat message — without unbounded memory growth on long-running
     servers (federal-tier safety).
  2. Single-flights concurrent first-turns per agent_did via a per-DID
     ``asyncio.Lock`` so two concurrent loads of *different* agents do
     not serialise through one global lock.
  3. Registers the agent with ``app.state.agent_registry`` on first
     load so the LIVE count, /api/agents, and the Agent Fleet page all
     reflect reality.
  4. Stays out of the way: ArcAgent's existing trace / session / module
     bus pipelines all fire normally — we only add an upstream cache
     and a registration hook.

Module boundary: this module is owned by arcui and may import
``arcgateway`` and the in-process ArcAgent because the embedded gateway
itself runs inside arcui's process. arcgateway/arcagent must not import
us. The wiring direction is one-way (arcui → embedded gateway → agents).
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from arcui.types import AgentRegistration

logger = logging.getLogger(__name__)

# LRU cap on the embedded agent cache. Each ArcAgent costs ~50MB resident
# (LLM model handle, tool registry, module bus); 32 entries ≈ 1.6 GB
# upper bound on a node serving a moderate fleet. Tunable per deployment
# tier later if needed.
_DEFAULT_CACHE_MAX = 32


class _BoundedAgentCache:
    """LRU cache for loaded ArcAgents with per-agent locks.

    Lookup is O(1) on cache hit. Eviction is O(1) (popitem on the
    oldest key). The lock map evicts in lockstep with the agent map so
    a re-load of an evicted agent gets a fresh lock, not a stale one.
    """

    def __init__(self, maxsize: int = _DEFAULT_CACHE_MAX) -> None:
        self._maxsize = maxsize
        self._agents: OrderedDict[str, Any] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    def __contains__(self, agent_did: str) -> bool:
        return agent_did in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    def get(self, agent_did: str) -> Any | None:
        agent = self._agents.get(agent_did)
        if agent is not None:
            self._agents.move_to_end(agent_did)
        return agent

    def lock_for(self, agent_did: str) -> asyncio.Lock:
        """Return the per-agent lock — created lazily on first request."""
        lock = self._locks.get(agent_did)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[agent_did] = lock
        return lock

    def put(self, agent_did: str, agent: Any) -> None:
        self._agents[agent_did] = agent
        self._agents.move_to_end(agent_did)
        while len(self._agents) > self._maxsize:
            evicted_did, _ = self._agents.popitem(last=False)
            self._locks.pop(evicted_did, None)
            logger.info(
                "embedded_agents: evicted %s from LRU (size=%d)",
                evicted_did,
                self._maxsize,
            )


def install_embedded_agent_hooks(
    app: Any, *, cache_maxsize: int = _DEFAULT_CACHE_MAX
) -> None:
    """Wrap the embedded executor's agent_factory with cache + registry hooks.

    Idempotent — the second call is a no-op so re-running this on a
    lifespan restart does not double-wrap. Uses the executor's public
    ``agent_factory`` property + ``set_agent_factory`` setter so we
    never touch private attributes.
    """
    bundle = getattr(app.state, "embedded_gateway", None)
    if bundle is None or bundle.executor is None:
        return
    executor = bundle.executor
    original_factory = getattr(executor, "agent_factory", None)
    if original_factory is None or getattr(executor, "_arcui_wrapped", False):
        return

    cache = _BoundedAgentCache(maxsize=cache_maxsize)

    async def factory(agent_did: str) -> Any:
        cached = cache.get(agent_did)
        if cached is not None:
            return cached
        # Single-flight per agent_did — two concurrent first-turns for
        # the SAME agent share one load; concurrent first-turns for
        # different agents proceed in parallel.
        async with cache.lock_for(agent_did):
            cached = cache.get(agent_did)
            if cached is not None:
                return cached
            agent = await original_factory(agent_did)
            cache.put(agent_did, agent)
            try:
                _register_in_fleet(app, agent_did, agent)
            except Exception:
                logger.exception(
                    "embedded_agents: failed to register %s in fleet",
                    agent_did,
                )
            return agent

    executor.set_agent_factory(factory)
    executor._arcui_wrapped = True
    app.state.embedded_agent_cache = cache


def _register_in_fleet(app: Any, agent_did: str, agent: Any) -> None:
    """Mark a chat-loaded agent as LIVE in app.state.agent_registry.

    Uses the agent name as registry key — same convention the WS-based
    registration uses, so the team_roster online overlay matches.
    """
    registry = getattr(app.state, "agent_registry", None)
    if registry is None:
        return
    name = _agent_name(agent, agent_did)
    if registry.get(name) is not None:
        return  # already registered (e.g. via /api/agent/connect WS)

    registration = AgentRegistration(
        agent_id=name,
        agent_name=name,
        model=_agent_attr(agent, "model", "unknown"),
        provider=_agent_attr(agent, "provider", "unknown"),
        connected_at=datetime.now(tz=UTC).isoformat(),
        meta={"source": "embedded_chat"},
    )
    registry.register(name, ws=None, registration=registration)
    logger.info("embedded_agents: registered %s as LIVE", name)


def _agent_name(agent: Any, agent_did: str) -> str:
    """Best-effort name for the registry key.

    Prefer ``agent._config.agent.name``; fall back to the last segment of
    the DID. The same logic the chat_ws route uses to resolve agent_did
    from the roster, in reverse.
    """
    cfg = getattr(agent, "_config", None)
    if cfg is not None:
        agent_section = getattr(cfg, "agent", None)
        if agent_section is not None:
            name = getattr(agent_section, "name", None)
            if name:
                return str(name)
    if "/" in agent_did:
        return agent_did.rsplit("/", 1)[-1]
    return agent_did.rsplit(":", 1)[-1]


def _agent_attr(agent: Any, name: str, default: str) -> str:
    """Pull a string attribute from the agent's LLM config, or default."""
    cfg = getattr(agent, "_config", None)
    if cfg is None:
        return default
    llm = getattr(cfg, "llm", None)
    if llm is None:
        return default
    value = getattr(llm, name, None)
    if isinstance(value, str) and value:
        return value
    # ``model`` is sometimes ``provider/model`` — split when caller wants ``provider``.
    if name == "provider":
        model = getattr(llm, "model", "")
        if isinstance(model, str) and "/" in model:
            return model.split("/", 1)[0]
    return default
