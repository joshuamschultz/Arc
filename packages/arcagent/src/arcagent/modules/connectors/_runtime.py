"""Per-agent connector module runtime context — SPEC-062 COMP-015 (T-901/T-902).

The decorator-form connector module (``capabilities.py``) cannot carry state in a
closure — ``@capability`` classes are instantiated by the loader with no arguments —
so runtime state lives on a :class:`_State` instance bound to a
:class:`contextvars.ContextVar`, configured by the agent at startup.

Task 27/32: a plain module global here is silently overwritten by whichever agent's
``asyncio.Task`` most recently called ``configure()`` — see
``arcagent/builtins/capabilities/_runtime.py`` for the full rationale, and
``modules/scheduler/_runtime.py`` for the pattern this module follows. A fleet of
agents sharing one process would otherwise leak one agent's connector state (which
extensions are attached, which tier applies, which policy pipeline gates a call) into
another's — unacceptable for a module whose entire job is governing external calls.

``configure()`` requests only the narrow set of kwargs COMP-015 declares as its
inputs (module config, agent identity, tier, policy pipeline, human gate) plus the
two nearly-universal ones every module needs (telemetry, workspace). It does not name
``operator_signer``, ``bus``, ``agent_run_fn``, or any of the other kwargs
``core/agent_lifecycle.py``'s signature-dispatched ``configure_module_runtimes`` makes
available — core offers signing authority and the rest only to modules that ask for
them, so this module asks for nothing it does not yet use.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.modules.connectors.config import ConnectorsConfig

if TYPE_CHECKING:
    from arctrust import AgentIdentity


@dataclass
class _State:
    """Immutable-in-practice runtime state shared across the connector module."""

    config: ConnectorsConfig
    workspace: Path
    telemetry: Any
    identity: AgentIdentity
    tier: str = "personal"
    policy_pipeline: Any = None
    human_gate: Any = None


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_connectors_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | ConnectorsConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    identity: AgentIdentity,
    tier: str = "personal",
    policy_pipeline: Any = None,
    human_gate: Any = None,
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup."""
    cfg = config if isinstance(config, ConnectorsConfig) else ConnectorsConfig(**(config or {}))
    _state_var.set(
        _State(
            config=cfg,
            workspace=workspace.resolve(),
            telemetry=telemetry,
            identity=identity,
            tier=tier,
            policy_pipeline=policy_pipeline,
            human_gate=human_gate,
        )
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "connectors module has not been configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return current


def bind(state_obj: _State) -> None:
    """Idempotently bind an already-built ``_State`` into the CURRENT task.

    Cheap — one ``.set()`` call, no construction. Called at the top of every
    turn-dispatch entry point (task 27 follow-up hotfix) so a turn running in a
    fresh sibling ``asyncio.Task`` — not a descendant of the task that ran
    ``configure()`` — still sees this agent's state.
    """
    _state_var.set(state_obj)


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


__all__ = ["bind", "configure", "reset", "state"]
