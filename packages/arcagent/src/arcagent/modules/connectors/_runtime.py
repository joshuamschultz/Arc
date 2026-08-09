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
two nearly-universal ones every module needs (telemetry, workspace), and the two
this module cannot do its job without: ``tool_registry``, which owns the dispatch
envelope every connector verb must ride, and ``config_path``, whose DIRECTORY NAME is
what a grant names — the workspace is a different directory, and an agent identified
by the wrong name is an agent matched against the wrong grants.

It also names ``operator_signer``, and for one read only: its ``public_key`` is
the key an extension bundle's ``.arcsig`` is pinned against (REQ-283), the same
key ``arc connector`` pins to, so a bundle signed by the deployment operator
verifies at agent start too. Nothing in this module signs anything. It does not
name ``bus``, ``agent_run_fn``, or any of the other kwargs
``core/agent_lifecycle.py``'s signature-dispatched ``configure_module_runtimes``
makes available — core offers those only to modules that ask, so this module asks
for nothing it does not use.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.modules.connectors.config import ConnectorsConfig

if TYPE_CHECKING:
    from arctrust import AgentIdentity

    from arcagent.core.tool_registry import ToolRegistry


@dataclass
class _State:
    """Immutable-in-practice runtime state shared across the connector module."""

    config: ConnectorsConfig
    workspace: Path
    telemetry: Any
    identity: AgentIdentity
    config_path: Path = Path("arcagent.toml")
    tool_registry: ToolRegistry | None = None
    operator_signer: Any = None
    tier: str = "personal"
    policy_pipeline: Any = None
    human_gate: Any = None

    @property
    def agent_dir(self) -> Path:
        """The directory holding ``arcagent.toml``. Its NAME is what a grant names."""
        return self.config_path.parent

    @property
    def arc_dir(self) -> Path:
        """The deployment root holding this deployment's connections and grants.

        Resolved here rather than at the read site so the agent, the CLI, the TUI
        and the web all land on one directory: a second spelling would mean the
        agent asked a file no surface ever wrote to, and answered "no grants" for
        every connection an operator had made.
        """
        configured = self.config.arc_dir
        if configured:
            return Path(configured).expanduser()
        from arctrust.paths import arc_home

        return arc_home()


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_connectors_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | ConnectorsConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    identity: AgentIdentity,
    config_path: Path = Path("arcagent.toml"),
    tool_registry: ToolRegistry | None = None,
    operator_signer: Any = None,
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
            config_path=Path(config_path).resolve(),
            tool_registry=tool_registry,
            operator_signer=operator_signer,
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
