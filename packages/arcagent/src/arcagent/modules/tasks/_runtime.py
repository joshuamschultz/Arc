"""Per-agent tasks module runtime context.

The decorator-form tasks module (``capabilities.py``) cannot carry state
in a closure — ``@tool`` stamps wrap plain functions — so runtime state
(store, config, telemetry, identity, registry) lives on a :class:`_State`
instance bound to a :class:`contextvars.ContextVar`, configured by the
agent at startup.

Task 27/32: a plain module global here is silently overwritten by
whichever agent's ``asyncio.Task`` most recently called ``configure()`` —
see ``arcagent/builtins/capabilities/_runtime.py`` for the full rationale.

``configure()`` is SYNC, matching every other module template: the real
dispatcher (``core.agent_lifecycle.configure_module_runtimes``) calls
``configure_fn(**kwargs)`` without ``await`` and has no ``registry`` kwarg
in its available set. An earlier revision of this module made ``configure()``
async so it could open the SQLite backend eagerly — that silently no-oped in
production (the coroutine was constructed but never scheduled). Fixed by
mirroring the messaging module's ``ensure_live_backend`` pattern instead:
``configure()`` only stores plain state; :func:`ensure_store` — idempotent,
awaited by every tool on first use — does the actual async I/O (opening the
arcstore backend, and lazily building a live registry when none was
injected).
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.modules.tasks.config import TasksConfig
from arcagent.modules.tasks.store import open_store

if TYPE_CHECKING:
    from arcteam.registry import EntityRegistry
    from arctrust import AgentIdentity


@dataclass
class _State:
    """Mutable runtime state shared across the tasks module's tools."""

    config: TasksConfig
    workspace: Path
    telemetry: Any
    identity: AgentIdentity
    # arcteam/arcstore service objects — injectable for tests via configure(),
    # otherwise built lazily by ensure_store() (SDD §3). Typed Any, mirroring
    # messaging's _runtime: no hard import-time dependency on the optional
    # arcteam package, and no dataclass-wide Optional narrowing for a field
    # that starts unset and is filled in exactly once. None means "not built
    # yet" — registry additionally means "unavailable" (assign_task and
    # create_task's owner-ref path degrade with a clear error rather than
    # crash or silently build a useless, disconnected in-memory registry).
    registry: Any = None
    store: Any = None
    # arcteam ``MessagingService`` used by ``assign_task`` to notify the
    # assignee (SDD §5, Phase C) — injectable for tests, otherwise built
    # lazily by ensure_store() over the same shared backend as ``registry``.
    # None means "not built yet"; a served agent with no ``nats_url`` never
    # gets one (assign_task then skips notify — no live inbox to deliver to).
    messenger: Any = None


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_tasks_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | TasksConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    identity: AgentIdentity,
    registry: Any = None,
    messenger: Any = None,
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup.

    Synchronous by contract (see module docstring) — no I/O happens here.
    """
    cfg = config if isinstance(config, TasksConfig) else TasksConfig(**(config or {}))
    ws = workspace.resolve()
    _state_var.set(
        _State(
            config=cfg,
            workspace=ws,
            telemetry=telemetry,
            identity=identity,
            registry=registry,
            messenger=messenger,
        )
    )


async def ensure_store() -> None:
    """Idempotent: open the arcstore backend and, if live, a real registry.

    Mirrors messaging's ``ensure_live_backend``. Runs at most once per agent
    — every tool awaits this before touching ``st.store``/``st.registry`` so
    the first tool call after a real (sync, un-awaited) ``configure()`` still
    ends up with a working store.
    """
    st = state()
    if st.store is not None:
        return
    st.store = await open_store(st.config.data_dir)
    if st.registry is None and st.config.nats_url:
        st.registry, st.messenger = await _build_live_services(st.config.nats_url, st.identity)


async def _build_live_services(
    nats_url: str, identity: AgentIdentity
) -> tuple[EntityRegistry, Any]:
    """Build a read-only ``EntityRegistry`` and a sending ``MessagingService``.

    Both share the one NATS backend connection (assign_task's ``@handle``
    -> DID resolve on the registry, and its ``TASK_ASSIGNED`` notify on the
    messenger, must see the same entity/stream state). Only
    ``resolve()``/``list_entities()`` (read paths) are ever called through
    the registry — which never touch the audit log (only
    ``register``/``update`` do, per ``arcteam.registry.EntityRegistry``). An
    ephemeral, never-signing operator key therefore satisfies
    ``AuditLogger``'s constructor without holding real audit authority;
    nothing is ever written through the registry, so there is no
    audited-subject-is-its-own-authority concern (SPEC-053) to inherit
    ``agent._operator_signer`` for. The messenger signs outbound
    notifications with the agent's own identity (REQ-030), mirroring the
    messaging module's ``_bootstrap.message_signer``.
    """
    from arcteam.audit import AuditLogger
    from arcteam.messenger import MessagingService
    from arcteam.registry import EntityRegistry
    from arctrust import OperatorKey

    from arcagent.modules.messaging._bootstrap import make_backend, message_signer

    backend = await make_backend(nats_url)
    audit = AuditLogger(backend, OperatorKey.generate().into_signer())
    registry = EntityRegistry(backend, audit)
    messenger = MessagingService(backend, registry, audit, signer=message_signer(identity))
    return registry, messenger


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "tasks module called before runtime is configured; "
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


__all__ = ["bind", "configure", "ensure_store", "reset", "state"]
