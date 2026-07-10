"""Per-agent proactive module runtime context.

The decorator-form proactive module (``capabilities.py``) cannot carry
state in a closure — ``@hook`` and ``@capability`` stamps wrap plain
functions/classes and the capability class is instantiated by the
loader with no arguments. Runtime state (engine, leader, config,
telemetry) therefore lives on a :class:`_State` instance bound to a
:class:`contextvars.ContextVar`, configured by the agent at startup.

Task 27/32: a plain module global here is silently overwritten by
whichever agent's ``asyncio.Task`` most recently called ``configure()`` —
see ``arcagent/builtins/capabilities/_runtime.py`` for the full rationale.
Safe for the tick loop too: it's spawned via ``asyncio.create_task()``
from ``ProactiveCapability.setup()``, which runs AFTER ``configure()`` in
the same agent-startup task — asyncio's automatic context-copy on task
creation gives the tick loop this agent's state (including ``_tick_task``
itself, mutated in place on the SAME ``_State`` instance) for its whole
lifetime.
"""

from __future__ import annotations

import contextvars
import logging
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcagent.core.telemetry import AgentTelemetry
    from arcagent.modules.proactive.engine import ProactiveEngine
    from arcagent.modules.proactive.leader import LeaderElection

_logger = logging.getLogger("arcagent.modules.proactive._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across the proactive capability + hooks."""

    config: dict[str, Any]
    workspace: Path
    telemetry: AgentTelemetry | None
    agent_name: str
    llm_config: Any
    leader: LeaderElection
    engine: ProactiveEngine | None = None
    # asyncio task handle for the running tick loop; stored so teardown
    # can cancel it without the capability needing to track it externally.
    _tick_task: Any = None


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_proactive_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: AgentTelemetry | None = None,
    workspace: Path = Path("."),
    agent_name: str = "",
    llm_config: Any = None,
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup.

    The leader-election backend is selected from ``config['leader']`` — see
    :func:`_build_leader`. It defaults to NoOp (single-instance / personal
    tier); a multi-instance enterprise / federal deployment MUST set
    ``leader='redis'`` or ``leader='k8s'`` so exactly one replica ticks the
    engine (R-048).
    """
    cfg = config or {}
    _state_var.set(
        _State(
            config=cfg,
            workspace=workspace.resolve(),
            telemetry=telemetry,
            agent_name=agent_name,
            llm_config=llm_config,
            leader=_build_leader(cfg, agent_name),
        )
    )


def _build_leader(config: dict[str, Any], agent_name: str) -> LeaderElection:
    """Select the leader-election backend from module config (R-048).

    ``config['leader']`` picks the implementation:

      * ``noop`` (default) — single-instance / personal tier; always elected.
      * ``redis`` — Redis ``SET NX PX`` lock; needs ``config['redis_url']``.
      * ``k8s``   — Kubernetes Lease; needs ``config['k8s_namespace']`` and
        ``config['k8s_lease_name']``.

    A multi-instance deployment that leaves this at NoOp self-elects on every
    replica and every replica ticks the engine. So an unknown backend, or a
    ``redis``/``k8s`` selection missing its required keys, raises here rather
    than silently degrading to NoOp — the deployment fails loud instead of
    quietly violating R-048.
    """
    backend = str(config.get("leader", "noop")).lower()
    if backend == "noop":
        from arcagent.modules.proactive.leader import NoOpLeaderElection

        return NoOpLeaderElection()
    identity = str(config.get("identity") or agent_name or socket.gethostname())
    if backend == "redis":
        from arcagent.modules.proactive.leader_redis import RedisLockElection

        url = config.get("redis_url")
        if not url:
            raise ValueError("proactive leader='redis' requires config['redis_url']")
        try:
            import redis.asyncio as redis_asyncio  # type: ignore[import-not-found]  # reason: optional dep
        except ImportError as err:
            raise RuntimeError(
                "proactive leader='redis' requires the 'redis' package. "
                "Install with 'pip install redis'."
            ) from err
        client = redis_asyncio.from_url(str(url))
        key = str(config.get("redis_key", "arcagent:proactive:leader"))
        return RedisLockElection(redis=client, key=key, identity=identity)
    if backend == "k8s":
        from arcagent.modules.proactive.leader_k8s import KubernetesLeaseElection

        namespace = config.get("k8s_namespace")
        lease_name = config.get("k8s_lease_name")
        if not namespace or not lease_name:
            raise ValueError(
                "proactive leader='k8s' requires config['k8s_namespace'] "
                "and config['k8s_lease_name']"
            )
        return KubernetesLeaseElection(
            namespace=str(namespace), lease_name=str(lease_name), identity=identity
        )
    raise ValueError(f"unknown proactive leader backend: {backend!r}")


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "proactive module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return current


def bind(state_obj: _State) -> None:
    """Idempotently bind an already-built ``_State`` into the CURRENT task.

    Cheap — one ``.set()`` call, no construction. Called at the top of
    every turn-dispatch entry point (task 27 follow-up hotfix) so a turn
    running in a fresh sibling ``asyncio.Task`` — not a descendant of the
    task that ran ``configure()`` — still sees this agent's state.
    """
    _state_var.set(state_obj)


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


__all__ = ["bind", "configure", "reset", "state"]
