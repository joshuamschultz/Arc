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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

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
    # arcteam's ``DefinitionStore`` — the read half. The control plane owns every
    # MUTATION; reads (list, inspect, version history) go straight to the store
    # because routing a read through a mutation surface buys nothing.
    definitions: Any = None
    # The fleet's single ``WorkflowRunner``, injected by COMP-009's RunnerHost.
    # None means no runner is hosted in this process — authoring still works.
    runner: Any = None
    # Deployment tier, handed to the control plane at construction. Stringency
    # metadata, never a gate (ADR-019).
    tier: str = "personal"
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


#: The process's workflow runner, published by the gateway's RunnerHost.
#:
#: A PROCESS global rather than a contextvar, and deliberately so: the runner is
#: a fleet singleton shared by every agent in this process (REQ-231), unlike the
#: per-agent state below. It also decouples publish order from configure order —
#: the gateway may start the runner before or after any agent configures its
#: module, and either way the runner is found. Getting that ordering wrong is
#: silent: the gateway hosts a live runner and every tool still refuses to run.
_process_runner: Any = None


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
    definitions: Any = None,
    tier: str = "personal",
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
            definitions=definitions,
            tier=tier,
            # Pick up a runner the gateway already published. Without this, an
            # agent configured AFTER the gateway started the runner would report
            # "no runner hosted here" on a deployment that has one running.
            runner=_process_runner,
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
        # The run store is opened HERE, in the async seam, because the control
        # plane needs it for purge's orphan guard and opening a backend is I/O —
        # which must never happen in the sync configure() (see module docstring).
        from arcagent.modules.workflows.run_store import open_run_store

        runs = await open_run_store(str(st.config.data_dir or ""))
        _build_control_plane(st, runs)


def set_runner(runner: Any) -> None:
    """Inject the process's workflow runner (COMP-009's RunnerHost calls this).

    The runner is a FLEET singleton hosted on the agent side of the gateway
    service; an agent must never construct its own, or two runners would advance
    the same frontier (REQ-231). Until one is injected, authoring works fully and
    ``workflow_run`` reports that no runner is hosted here — which is the honest
    state of a deployment whose gateway has not started one.
    """
    global _process_runner
    _process_runner = runner
    st = _state_var.get()
    if st is None:
        # Published before any agent configured its module — legitimate ordering,
        # and configure() will pick it up. Refusing here would drop the runner on
        # the floor with only a warning, which is the silent-failure shape this
        # whole seam keeps producing.
        return
    st.runner = runner
    # Force a rebuild so the control plane picks up the live runner.
    st.control_plane = None
    st.build_attempted = False


class _NoRunner:
    """Stands in for an absent runner so authoring still works standalone."""

    _MESSAGE = (
        "no workflow runner is hosted in this process — a run is started by the "
        "fleet service's single runner (COMP-009), never by an agent"
    )

    async def start_run(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(self._MESSAGE)

    async def cancel(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(self._MESSAGE)


def _operator_public_key() -> bytes | None:
    """The deployment operator's verify key, or None when absent.

    None is fail-closed at enterprise/federal (the store refuses to run an
    unsigned or foreign-signed definition) and audit-warn at personal, which is
    the same posture arcprompt uses for overlays.
    """
    try:
        from arctrust import OperatorKey, default_operator_key_path

        return OperatorKey.load(default_operator_key_path(), generate_if_absent=False).public_key
    except (OSError, ValueError, RuntimeError):
        return None


def _build_control_plane(st: _State, runs: Any) -> None:
    """Construct the control plane and definition store over the bundle root.

    Leaves both None when the workflow engine is absent. An ImportError must not
    escape: the capability loader would surface it as a broken module and take
    the agent's whole tool surface with it, when the honest outcome is "this
    deployment has no workflow engine" — which every tool reports clearly.
    """
    try:
        from arcteam.workflow import (
            DefinitionStore,
            parse_definition,
            validate_definition,
        )
        from arcteam.workflow.control_plane import WorkflowControlPlane
    except ImportError:
        _logger.info("arcteam.workflow is not installed; workflow tools will report unavailable")
        return

    root = st.workspace / st.config.workflows_dir
    root.mkdir(parents=True, exist_ok=True)
    # tier and the pinned operator key are LOAD-BEARING, not optional polish.
    # Omitting them made this store believe every deployment was personal-tier
    # with no pinned key — so an unsigned draft ran at federal, and worse, an
    # agent could sign a workflow with its OWN key and the store reported it
    # verified. That is exactly the attack the draft-then-operator-sign
    # lifecycle exists to prevent: with no pin, verification falls back to
    # trusting the key embedded in the sidecar (trust-on-first-use, the LLM03
    # hole SPEC-047 already closed for blueprints). REQ-225 requires refusal
    # above personal tier. The tier was already in scope — it is passed to the
    # control plane fifteen lines below.
    st.definitions = DefinitionStore(
        root=root,
        tier=st.tier,
        operator_public_key=_operator_public_key(),
    )

    def parse(document: Mapping[str, Any]) -> Any:
        return parse_definition(dict(document))

    # The control plane's constructor is nominally typed against arcteam's own
    # runner and tier literal. The runner here is either the injected fleet
    # singleton (structurally identical) or the local no-runner stand-in, and the
    # tier is a config string this deployment already validated — so both are
    # widened at this one seam rather than by loosening arcteam's contract.
    st.control_plane = WorkflowControlPlane(
        definitions=st.definitions,
        parse=parse,
        validate=validate_definition,
        runner=cast(Any, st.runner if st.runner is not None else _NoRunner()),
        runs=cast(Any, runs),
        tier=cast(Any, st.tier),
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
    """Test-only: clear runtime state AND the process runner.

    Both, or a runner published by one test leaks into the next and a genuinely
    unwired case would pass.
    """
    global _process_runner
    _process_runner = None
    _state_var.set(None)


__all__ = ["bind", "configure", "ensure_control_plane", "reset", "set_runner", "state"]
