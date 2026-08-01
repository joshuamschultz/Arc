"""Per-agent workflows module runtime context (SPEC-061 COMP-012).

Same shape as the tasks module: the decorator-form capability cannot carry
state in a closure, so runtime state lives on a :class:`_State` bound to a
:class:`contextvars.ContextVar` — a plain module global is silently overwritten
by whichever agent's ``asyncio.Task`` most recently called ``configure()``.

``configure()`` is SYNC by contract — ``core.agent_lifecycle`` calls every
module's ``configure_fn(**kwargs)`` without ``await``. An async configure would
construct a coroutine that is never scheduled and silently no-op in production.
All async wiring therefore happens in :func:`ensure_control_plane`, which every
tool awaits on first use.

``arcteam.workflow`` is imported lazily, inside the function that needs it
(the ``modules/messaging/_runtime.py`` precedent), so this module imports
cleanly on a deployment without the team package installed and degrades with a
clear error instead of an ImportError at capability-load time.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.modules.workflows.config import WorkflowsConfig

if TYPE_CHECKING:
    from arctrust import AgentIdentity

_logger = logging.getLogger("arcagent.modules.workflows._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across the workflows module's tools."""

    config: WorkflowsConfig
    workspace: Path
    identity: AgentIdentity
    telemetry: Any = None
    # arcteam's ``WorkflowControlPlane`` (COMP-021) — the ONE shared operation
    # set that the command line and the dashboard also call, so the three
    # authoring surfaces cannot drift. Typed ``Any``, mirroring the tasks
    # module's ``registry``/``store``: no import-time dependency on the optional
    # arcteam package. Injectable for tests; otherwise built lazily by
    # :func:`ensure_control_plane`. None means "not built yet".
    control_plane: Any = None
    # True once a build was attempted, so a deployment without arcteam.workflow
    # degrades with one clear error per call rather than retrying the import on
    # every tool invocation.
    build_attempted: bool = False
    # The operator human-approval gate (COMP-016 activation approval). Threaded
    # by agent_lifecycle to any module whose configure() asks for it.
    human_gate: Any = None
    # Serialises the lazy first-use build so two concurrent first tool calls
    # cannot both construct a control plane (REL-F4 check-then-act race).
    init_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Content hashes this agent has already obtained an activation grant for
    # (COMP-016). A narrowing edit whose leg union is a subset of an already
    # approved union must not re-prompt, so the approved UNIONS are kept too.
    approved_leg_unions: list[frozenset[str]] = field(default_factory=list)


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_workflows_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | WorkflowsConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    identity: AgentIdentity,
    human_gate: Any = None,
    control_plane: Any = None,
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at startup.

    Synchronous by contract (see module docstring) — no I/O happens here.
    ``control_plane`` is injectable so a test can drive the tool surface
    without a live arcteam; production leaves it None and
    :func:`ensure_control_plane` builds it on first use.
    """
    cfg = config if isinstance(config, WorkflowsConfig) else WorkflowsConfig(**(config or {}))
    _state_var.set(
        _State(
            config=cfg,
            workspace=workspace.resolve(),
            identity=identity,
            telemetry=telemetry,
            human_gate=human_gate,
            control_plane=control_plane,
        )
    )


async def ensure_control_plane() -> None:
    """Idempotent: build arcteam's workflow control plane on first use.

    Mirrors the tasks module's ``ensure_store``. Serialised under
    ``init_lock``; the re-check inside the lock is the build-once guard. A
    deployment without ``arcteam.workflow`` leaves ``control_plane`` None and
    every tool returns a clear unavailability error — the module never
    half-works, and it never silently constructs a disconnected local engine
    that would let one surface drift from the other two.
    """
    st = state()
    if st.control_plane is not None or st.build_attempted:
        return
    async with st.init_lock:
        if st.control_plane is not None or st.build_attempted:
            return
        st.build_attempted = True
        st.control_plane = await _build_control_plane(st)


async def _build_control_plane(st: _State) -> Any:
    """Construct the arcteam control plane over this agent's bundle root.

    Returns None when the workflow engine is absent. An ImportError must not
    escape: the capability loader would surface it as a broken module and take
    the agent's whole tool surface with it, when the honest outcome is "this
    deployment has no workflow engine" — which every tool reports clearly.
    """
    try:
        from arcteam.workflow import WorkflowControlPlane
    except ImportError:
        _logger.info("arcteam.workflow is not installed; workflow tools will report unavailable")
        return None

    return await WorkflowControlPlane.open(
        root=st.workspace / st.config.workflows_dir,
        data_dir=st.config.data_dir,
        actor_did=st.identity.did,
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "workflows module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return current


def bind(state_obj: _State) -> None:
    """Idempotently bind an already-built ``_State`` into the CURRENT task."""
    _state_var.set(state_obj)


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


__all__ = ["bind", "configure", "ensure_control_plane", "reset", "state"]
