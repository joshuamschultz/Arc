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
    # The deployment operator ``Signer`` — the ONE authority a workflow signature
    # may carry. Its public key is what the definition store pins against, so a
    # definition signed by any other key is not merely unverified, it is refused
    # above personal tier (REQ-225). The agent never gets the private half.
    operator_signer: Any = None
    # ``(event, payload)`` sink for the definition store's own audit events.
    # ``workflow.signed`` and ``workflow.unsigned_run_permitted`` can be emitted
    # by NOTHING else, so an unwired hook means an unsigned or self-signed
    # workflow leaves no record anywhere.
    audit_hook: Any = None
    # Serialises the lazy first-use build so two concurrent first tool calls
    # cannot both construct a control plane (REL-F4 check-then-act race).
    init_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Handles the team registry knows, refreshed before each authoring call.
    # A definition naming an agent that does not exist validates fine and then
    # dies at its first node with "unknown agent" — the roster is what turns
    # that into a repairable authoring error (REQ-219).
    known_agents: frozenset[str] = frozenset()
    # arcteam ``EntityRegistry``, built lazily over the shared bus. None means
    # no roster is available, and the check is skipped rather than guessed.
    registry: Any = None
    # True once a registry build was attempted. A box with no team bus pays the
    # connect timeout ONCE, not on every authoring call.
    roster_attempted: bool = False
    # Content hashes this agent has already obtained an activation grant for
    # (COMP-016). A narrowing edit whose leg union is a subset of an already
    # approved union must not re-prompt, so the approved UNIONS are kept too.
    approved_leg_unions: list[frozenset[str]] = field(default_factory=list)


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_workflows_state", default=None
)


@dataclass
class _HostedRunner:
    """Process-wide slot for the fleet's single runner (REQ-231).

    Deliberately NOT per-agent and deliberately not a ContextVar: one runner
    serves every agent in this process (the same process fact
    ``RunnerHost._active`` records), and the gateway publishes it during
    bootstrap — before, and from a different asyncio task than, the agents that
    later configure this module. A ContextVar handoff would drop it silently and
    every ``workflow_run`` would refuse on a deployment that is running one.

    Mutated in place rather than rebound, so per-AGENT state here stays where it
    belongs: on the ContextVar-held ``_State``.
    """

    runner: Any = None


_hosted = _HostedRunner()


def configure(
    *,
    config: dict[str, Any] | WorkflowsConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    identity: AgentIdentity,
    human_gate: Any = None,
    operator_signer: Any = None,
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
    audit = getattr(telemetry, "audit_event", None)
    _state_var.set(
        _State(
            config=cfg,
            workspace=workspace.resolve(),
            identity=identity,
            telemetry=telemetry,
            human_gate=human_gate,
            operator_signer=operator_signer,
            audit_hook=audit,
            control_plane=control_plane,
            definitions=definitions,
            tier=tier,
            # Adopt a runner the gateway already published: an agent that binds
            # its module after bootstrap must still reach the live runner.
            runner=_hosted.runner,
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
        # Opened HERE, in the async seam: the control plane needs the run store
        # for purge's orphan guard, and backend I/O must never happen in the
        # sync configure().
        from arcagent.modules.workflows.run_store import open_run_store

        runs = await open_run_store(str(st.config.data_dir or ""))
        _build_control_plane(st, runs)


async def refresh_roster(nats_url: str = "") -> None:
    """Refresh the handles the authoring check validates against.

    Best-effort by design: a deployment with no team bus has no roster, and the
    validator skips a kind it has no roster for rather than rejecting every
    node. What must not happen is the opposite — a definition naming an agent
    nobody has ever registered passing validation and dying at its first node.
    """
    st = state()
    url = nats_url or st.config.nats_url
    if st.registry is None and (not url or st.roster_attempted):
        return
    try:
        if st.registry is None:
            st.roster_attempted = True
            from arcteam.audit import AuditLogger
            from arcteam.registry import EntityRegistry

            from arcagent.core.arcteam_bootstrap import make_backend

            backend = await make_backend(url)
            audit = AuditLogger(backend, st.operator_signer)
            await audit.initialize()
            st.registry = EntityRegistry(backend, audit)
        entities = await st.registry.list_entities()
    except Exception:  # reason: no roster is a skipped check, never a failure
        _logger.debug("workflow roster unavailable; agent references go unchecked", exc_info=True)
        return
    handles = {f"@{e.handle}" for e in entities if getattr(e, "handle", "")}
    st.known_agents = frozenset(handles)


def set_runner(runner: Any) -> None:
    """Inject the process's workflow runner (COMP-009's RunnerHost calls this).

    The runner is a FLEET singleton hosted on the agent side of the gateway
    service; an agent must never construct its own, or two runners would advance
    the same frontier (REQ-231). Until one is injected, authoring works fully and
    ``workflow_run`` reports that no runner is hosted here — which is the honest
    state of a deployment whose gateway has not started one.

    Publishing survives BOTH orderings. The gateway may publish before any agent
    has configured this module (bootstrap starts the runner first), so the
    runner is recorded process-wide either way and adopted by every later
    ``configure``.
    """
    _hosted.runner = runner
    current = _state_var.get()
    if current is None:
        return
    current.runner = runner
    # Force a rebuild so the control plane picks up the live runner.
    current.control_plane = None
    current.build_attempted = False


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


def _operator_public_key(st: _State) -> bytes | None:
    """The deployment operator pubkey every workflow signature is pinned against.

    Taken from the operator ``Signer`` the agent already resolved, rather than
    re-read from disk: that is the same authority the human gate pins approvals
    to, and a second resolution is a second chance to pin a different key.

    Returns None only when no operator signer exists at all. That is not a
    fallback — with no pinned key the store refuses fail-closed above personal
    tier, which is the correct outcome and far better than the silent pass that
    an unpinned verification gives.
    """
    signer = st.operator_signer
    if signer is None:
        _logger.warning(
            "no operator signer available; workflow signatures cannot be pinned and "
            "definitions will be refused above personal tier (fail-closed)"
        )
        return None
    key: bytes = signer.public_key
    return key


def _bundle_root(st: _State) -> Path:
    """Where this deployment's workflow bundles live.

    One root per deployment, shared with ``arc workflow`` and the fleet runner.
    Resolving it under the agent's workspace instead is the shape that made an
    agent-authored workflow real on disk and invisible to everything that could
    sign or run it.
    """
    configured = Path(st.config.workflows_dir)
    if configured.is_absolute():
        return configured
    from arcteam.config import default_config_dir

    return default_config_dir() / configured


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

    root = _bundle_root(st)
    root.mkdir(parents=True, exist_ok=True)
    # Every argument here is load-bearing, and every default is dangerous.
    # Omitting ``tier`` makes the store believe it is a personal deployment at
    # EVERY tier, so REQ-225's refusal of an unsigned definition never fires.
    # Omitting ``operator_public_key`` makes ``verify_artifact`` fall back to
    # trusting the key embedded in the sidecar — trust-on-first-use — so a
    # definition signed by ANY key an agent holds reports as signed and
    # verified, which is precisely the self-blessing the draft-then-operator-sign
    # lifecycle exists to prevent (LLM03/LLM06/ASI04). Neither omission is
    # visible on the happy path: a correctly signed workflow at personal tier
    # behaves identically either way.
    st.definitions = DefinitionStore(
        root=root,
        tier=st.tier,
        operator_public_key=_operator_public_key(st),
        audit=st.audit_hook,
    )

    def parse(document: Mapping[str, Any]) -> Any:
        return parse_definition(dict(document))

    def validate(definition: Any, *, pending_files: frozenset[str] = frozenset()) -> Any:
        from arcteam.workflow.validator import KnownReferences

        return validate_definition(
            definition,
            known=KnownReferences(agents=st.known_agents),
            bundle_root=root,
            pending_files=pending_files,
        )

    # The control plane's constructor is nominally typed against arcteam's own
    # runner and tier literal. The runner here is either the injected fleet
    # singleton (structurally identical) or the local no-runner stand-in, and the
    # tier is a config string this deployment already validated — so both are
    # widened at this one seam rather than by loosening arcteam's contract.
    st.control_plane = WorkflowControlPlane(
        definitions=st.definitions,
        parse=parse,
        validate=validate,
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
    """Test-only: clear runtime state, including the process-hosted runner."""
    _hosted.runner = None
    _state_var.set(None)


__all__ = [
    "bind",
    "configure",
    "ensure_control_plane",
    "refresh_roster",
    "reset",
    "set_runner",
    "state",
]
