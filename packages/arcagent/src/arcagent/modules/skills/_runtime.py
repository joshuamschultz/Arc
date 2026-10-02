"""Per-agent skills-module runtime — wires the SkillAdapter seam (SPEC-044).

Mirrors :mod:`arcagent.modules.memory._runtime`. ``configure`` builds the injected
seams (the operator-anchored skill revision writer, operator-key WORM
:class:`~arctrust.AuditSink`, the eval
LLM bridged to a text-in/text-out invoker, the agent's :class:`~arcprompt.PromptSource`, and
the operator-approval provider bound to the shared :class:`HumanGate`) and selects
the :class:`~arcagent.skilladapt.SkillAdapter`. With a :class:`NullSkillAdapter`, ``active``
is ``False`` and every hook short-circuits — a silent no-op that writes nothing (AC-1).

The ``skill_path`` seam and the retire/revive suppression reconcile read the
per-task state lazily so the real
:class:`~arcagent.capabilities.capability_registry.CapabilityRegistry`
delivered at ``agent:ready`` is visible without rebinding the adapter.

Task 27/32: state is bound to a :class:`contextvars.ContextVar`, not a
plain module global — a plain global is silently overwritten by whichever
agent's ``asyncio.Task`` most recently called ``configure()``; see
``arcagent/builtins/capabilities/_runtime.py`` for the full rationale.
The Curator's ``@background_task`` lifecycle-sweep loop is safe under
this: it's spawned via ``asyncio.create_task()`` after ``configure()`` in
the same agent-startup task, so asyncio's automatic context-copy on task
creation gives it this agent's state for its whole lifetime.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcprompt import PromptSource

from arcagent.core.config import EvalConfig
from arcagent.modules.skills.outcome import OneShotInvoker, OutcomeClassifier
from arcagent.skilladapt import LLMInvoker, NullSkillAdapter, SkillAdapter, select_skill_adapter
from arcagent.utils.model_helpers import get_eval_model

_logger = logging.getLogger("arcagent.modules.skills._runtime")


@dataclass
class _TurnState:
    turn_number: int | None = None
    active_skill: str | None = None
    error_counts: dict[str, int] = field(default_factory=dict)
    llm_trace_id: str | None = None
    seen_calls: OrderedDict[str, None] = field(default_factory=OrderedDict)

    def accept_call(self, call_id: str) -> bool:
        if not call_id:
            return True
        if call_id in self.seen_calls:
            return False
        self.seen_calls[call_id] = None
        if len(self.seen_calls) > 4096:
            self.seen_calls.popitem(last=False)
        return True


@dataclass
class _State:
    adapter: SkillAdapter
    active: bool
    workspace: Path
    telemetry: Any = None
    # The real CapabilityRegistry (delivered at agent:ready). Skills live in its ``_skills``
    # dict as SkillEntry(name=, location=, ...) — NOT the old SkillRegistry ``.skills`` shape.
    skill_registry: Any = None
    # Signal-extraction state (the split-off half of the old trace_collector):
    # resolved SKILL.md path -> skill name, and the currently active skill span.
    skill_paths: dict[Path, str] = field(default_factory=dict)
    turns: OrderedDict[tuple[str, str], _TurnState] = field(default_factory=OrderedDict)
    closed_turns: OrderedDict[tuple[str, str], None] = field(default_factory=OrderedDict)
    # Turn-end outcome classifier (SPEC-054 REQ-115/116).
    outcome_classifier: OutcomeClassifier | None = None
    # Curator lifecycle-sweep cadence (CRITICAL-1): how often the @background_task loop
    # wakes. The 30-day inactivity *window* lives in the improver's LifecycleConfig.
    sweep_poll_seconds: float = 3_600.0
    sweep_turn: int = 0
    last_turn: int = 0  # stashed from agent:post_plan so the off-loop sweep has a turn label

    def turn(self, session_id: str, run_id: str) -> _TurnState | None:
        key = (session_id, run_id)
        if key in self.closed_turns:
            return None
        if key not in self.turns:
            self.turns[key] = _TurnState()
            if len(self.turns) > 1024:
                self.turns.popitem(last=False)
        self.turns.move_to_end(key)
        return self.turns[key]

    def close_turn(self, session_id: str, run_id: str) -> None:
        key = (session_id, run_id)
        self.turns.pop(key, None)
        self.closed_turns[key] = None
        if len(self.closed_turns) > 4096:
            self.closed_turns.popitem(last=False)

    def begin_turn(self, session_id: str, run_id: str, turn_number: int | None) -> None:
        self.closed_turns.pop((session_id, run_id), None)
        turn = self.turn(session_id, run_id)
        if turn is not None:
            turn.turn_number = turn_number

    def resolve_skill_path(self, skill_name: str) -> Path | None:
        """Resolve a skill name to its SKILL.md via THIS agent's CapabilityRegistry.

        Bound to the state, not read from the context variable, so an operator
        surface calling the adapter from its own task (an arcui request) still
        resolves this agent's skills. The registry is read lazily, so the real
        :class:`~arcagent.capabilities.capability_registry.CapabilityRegistry`
        delivered at ``agent:ready`` is visible without rebinding the adapter.
        """
        if self.skill_registry is None:
            return None
        entry = self.skill_registry.skill_entry(skill_name)
        if entry is None:
            return None
        location: Path = entry.location
        return location

    def index_skills(self, registry: Any) -> None:
        """Rebuild the SKILL.md-path -> name lookup from the CapabilityRegistry."""
        self.skill_registry = registry
        self.skill_paths = {
            entry.location.resolve(): entry.name for entry in registry.skill_entries()
        }


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_skills_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | None = None,
    eval_config: EvalConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    llm_config: Any = None,
    agent_name: str = "",
    agent_did: str = "",
    operator_signer: Any = None,
    human_gate: Any = None,
    prompt_source: PromptSource | None = None,
    skill_revisions: Any = None,
    capability_reload: Callable[[], Coroutine[Any, Any, str]] | None = None,
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup.

    ``prompt_source`` is the agent's overlay-aware prompt lookup (ADR-033 dependency
    key), handed to the improver and the outcome classifier so an operator's signed
    prompt edit reaches the model. ``None`` (module configured outside an agent) leaves
    both on their shipped stock prompts.

    ``skill_revisions`` (the agent's anchored revision authority) plus
    ``operator_signer`` build the improver's ONE write path: every applied change is
    an operator-signed anchored revision. The agent's own DID key signs nothing here;
    without either, the improver refuses every write (fail closed).
    ``capability_reload`` re-scans the agent's capabilities after a commit.
    """
    from arcagent.modules.skills.approval import build_skill_approval_provider
    from arcagent.modules.skills.config import SkillsConfig

    cfg = SkillsConfig(**(config or {}))
    ws = workspace.resolve()
    audit_sink = _build_worm_sink(ws, operator_signer, telemetry)
    # Operator-approval seam (D-10): a thin provider bound to the SHARED HumanGate
    # (SPEC-035/043 — operator-signed, self-approval-guarded, fail-closed at federal). No
    # gate wired → None → the improver denies (fail-closed). The improver decides *when*
    # approval is required per the tier ladder.
    approval_provider = (
        build_skill_approval_provider(human_gate, agent_did) if human_gate is not None else None
    )
    llm = _eval_invoker(eval_config, llm_config, agent_name)
    new_state = _State(
        adapter=NullSkillAdapter(),
        active=False,
        workspace=ws,
        telemetry=telemetry,
        sweep_poll_seconds=cfg.sweep_poll_seconds,
        # Built even when the eval LLM is unavailable — classify() abstains without one,
        # keeping the flag's behavior fail-open instead of silently off.
        outcome_classifier=(
            OutcomeClassifier(llm=llm, prompt_source=prompt_source)
            if cfg.classify_outcomes
            else None
        ),
    )
    new_state.adapter = select_skill_adapter(
        cfg.adapter,
        workspace=ws,
        config=cfg.improver,
        tier=cfg.tier,
        llm=llm,
        writer=_build_writer(skill_revisions, operator_signer, new_state.resolve_skill_path),
        reload=_reload_trigger(capability_reload),
        approval_provider=approval_provider,
        audit_sink=audit_sink,
        agent_did=agent_did,
        skill_path=new_state.resolve_skill_path,
        adapter_allowlist=tuple(cfg.adapter_allowlist),
        prompt_source=prompt_source,
    )
    new_state.active = not isinstance(new_state.adapter, NullSkillAdapter)
    _state_var.set(new_state)
    _logger.info("skills module configured (adapter=%s, active=%s)", cfg.adapter, new_state.active)


def _build_writer(
    skill_revisions: Any,
    operator_signer: Any,
    skill_path: Callable[[str], Path | None],
) -> Any:
    """The operator-anchored revision writer, or None (the improver then refuses writes).

    Skill revisions live in the capability-import module. It is imported here, at the
    boundary, so this module still starts when that one is absent; no authority means
    no writer, never a fallback signer.
    """
    try:
        from arcagent.modules.capability_import.authority_factory import (
            build_operator_skill_writer,
        )
        from arcagent.modules.capability_import.revisions import installed_skill_folder
    except ImportError:
        return None

    def folder_of(skill_name: str) -> Path | None:
        location = skill_path(skill_name)
        return installed_skill_folder(location) if location is not None else None

    return build_operator_skill_writer(skill_revisions, operator_signer, folder_of)


def _reload_trigger(
    capability_reload: Callable[[], Coroutine[Any, Any, str]] | None,
) -> Callable[[], None] | None:
    """A sync reload hook for the improver that schedules the agent's async reload.

    The improver calls ``reload()`` synchronously from inside its own task; the
    agent's capability reload is async, so it runs as a tracked task on the loop.
    """
    if capability_reload is None:
        return None
    pending: set[asyncio.Task[str]] = set()

    def trigger() -> None:
        task = asyncio.get_running_loop().create_task(capability_reload())
        pending.add(task)
        task.add_done_callback(_finish_reload(pending))

    return trigger


def _finish_reload(pending: set[asyncio.Task[str]]) -> Callable[[asyncio.Task[str]], None]:
    def done(task: asyncio.Task[str]) -> None:
        pending.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _logger.warning(
                "capability reload after a skill revision failed: %s",
                type(task.exception()).__name__,
            )

    return done


def _eval_invoker(
    eval_config: EvalConfig | None, llm_config: Any, agent_name: str
) -> LLMInvoker | None:
    """The eval model bridged to the prompt-in/text-out seam, or ``None`` when unavailable.

    The improver and the outcome classifier send one prompt string and read text back;
    a model handle takes ``list[Message]`` and returns a response object. The handle is
    typed ``object`` here so mypy refuses it anywhere an :class:`LLMInvoker` is
    expected — only the :class:`OneShotInvoker` bridge satisfies the seam.
    """
    model: object | None = get_eval_model(
        cached_model=None,
        eval_config=eval_config or EvalConfig(),
        llm_config=llm_config,
        logger=_logger,
        agent_label=f"{agent_name}/skills" if agent_name else "skills",
    )
    return OneShotInvoker(model) if model is not None else None


def _build_worm_sink(workspace: Path, operator_signer: Any | None, telemetry: Any) -> Any:
    """Operator-signed WORM audit sink in ``<agent_root>/.audit/skills.worm`` (SPEC-053).

    Signed by the OPERATOR signer, never the agent DID — the audited subject must not
    be its own audit authority. Fail-open (AU-5): a sink that cannot be opened degrades
    to disabled rather than breaking startup.
    """
    if operator_signer is None:
        return None
    try:
        from arctrust import WormSink

        chain = workspace.parent / ".audit" / "skills.worm"
        preexisting = chain.exists()
        sink = WormSink(chain, operator_signer)
    except Exception:  # reason: fail-open — never break startup on audit setup
        _logger.warning("skills WORM audit sink unavailable; audit disabled")
        return None
    if preexisting and not sink.verify_chain() and telemetry is not None:
        telemetry.audit_event("skills.audit.chain_verify_failed", {"chain": str(chain)})
    return sink


async def run_lifecycle_sweep() -> None:
    """One Curator pass: retire/revive sweep through the adapter, then reconcile the
    registry so retired skills stop being offered (CRITICAL-1 producer body + HIGH-3).

    Driven by the ``@background_task`` loop — the sole producer of ``review_lifecycle``,
    never a direct facade call.
    """
    st = _state_var.get()
    if st is None or not st.active:
        return
    st.sweep_turn += 1
    turn_label = st.last_turn or st.sweep_turn
    await st.adapter.review_lifecycle(turn=turn_label)
    await st.adapter.sweep_suites()
    await st.adapter.review_consolidation(turn=turn_label)
    await reconcile_suppression()


async def reconcile_suppression() -> None:
    """Align the registry's suppressed set with the adapter's retired skills (HIGH-3).

    Suppress newly-retired skills (hide from the offering) and unsuppress revived ones.
    Called after each sweep and once at ``agent:ready`` so retirement survives restart
    (the retired set is read from the on-disk candidate-store manifest).
    """
    st = _state_var.get()
    if st is None or not st.active or st.skill_registry is None:
        return
    try:
        retired = st.adapter.retired_skills()
    except Exception:  # reason: fail-open — an adapter without lifecycle must not break ready
        _logger.warning("skill retire/revive reconcile unavailable", exc_info=True)
        return
    registry = st.skill_registry
    for name in retired:
        await registry.suppress_skill(name)
    for name in await registry.suppressed_skills():
        if name not in retired:
            await registry.unsuppress_skill(name)


def record_turn(turn: int) -> None:
    """Stash the latest turn number (from agent:post_plan) for the off-loop sweep label."""
    current = _state_var.get()
    if current is not None:
        current.last_turn = turn


def state() -> _State:
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "skills module called before runtime is configured; "
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


__all__ = [
    "bind",
    "configure",
    "reconcile_suppression",
    "record_turn",
    "reset",
    "run_lifecycle_sweep",
    "state",
]
