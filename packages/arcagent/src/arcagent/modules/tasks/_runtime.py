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
        st.registry = await _build_live_registry(st.config.nats_url)


async def _build_live_registry(nats_url: str) -> EntityRegistry:
    """Build a read-only ``EntityRegistry`` over the shared NATS backend.

    Only ``resolve()``/``list_entities()`` (read paths) are ever called
    through this registry — ``assign_task``'s ``@handle`` -> DID lookup —
    which never touch the audit log (only ``register``/``update`` do, per
    ``arcteam.registry.EntityRegistry``). An ephemeral, never-signing
    operator key therefore satisfies ``AuditLogger``'s constructor without
    holding real audit authority; nothing is ever written through this
    registry, so there is no audited-subject-is-its-own-authority concern
    (SPEC-053) to inherit ``agent._operator_signer`` for.
    """
    from arcteam.audit import AuditLogger
    from arcteam.registry import EntityRegistry
    from arctrust import OperatorKey

    from arcagent.modules.messaging._bootstrap import make_backend

    backend = await make_backend(nats_url)
    audit = AuditLogger(backend, OperatorKey.generate().into_signer())
    return EntityRegistry(backend, audit)


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
