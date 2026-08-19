"""Dynamic strategy: the model writes an orchestration script, the engine runs it.

For an ad-hoc task that deserves more than one linear ReAct chain, a single
model call authors a short script in a restricted subset; the interpreter walks
it deterministically and its ``agent()`` / ``parallel()`` calls become bounded
child runs through :class:`~arcrun.dynamic.binding.RunHost`.

The whole authoring path is fail-open by design. A script the model got wrong is
a planning miss, not a run-ending fault, so a rejected script degrades to the
ordinary loop after exactly one correction attempt. What is *not* fail-open is
the outcome: a paused or failed script is reported as such, never dressed up as
a completion.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import arcllm
from arcprompt import load_stock

from arcrun._messages import content_text, system_message, user_message
from arcrun.dynamic.binding import RunHost
from arcrun.dynamic.host import DEFAULT_AGENT_CALLS, ScriptOutcome
from arcrun.dynamic.interpreter import execute_script
from arcrun.dynamic.journal import Journal, JournalError, JournalTampered
from arcrun.dynamic.seal import RunSeal
from arcrun.dynamic.validate import dry_run
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies import Strategy
from arcrun.strategies.react import build_result, react_loop
from arcrun.types import LoopResult

# Authoring gets the first shot plus exactly one correction. A model that cannot
# produce a parseable script twice will usually not produce one on the third
# attempt either, and the ordinary loop is a perfectly good answer.
_AUTHOR_ATTEMPTS = 2

_logger = logging.getLogger(__name__)

# The validated script is pinned beside its journal: both describe one run,
# and a journal without the script that produced it cannot be replayed.
_SCRIPT_FILENAME = "script.py"
_JOURNAL_FILENAME = "journal.jsonl"

_EMIT_SCRIPT = arcllm.Tool(
    name="emit_script",
    description="Emit the orchestration script that solves this task.",
    parameters={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "The script source, in the restricted subset. No code fence.",
            },
            "reasoning": {
                "type": "string",
                "description": "One line on why the task needs this shape.",
            },
        },
        "required": ["source"],
    },
)


class DynamicStrategy(Strategy):
    """Author a script, validate it, run it — or fall back to the ReAct loop."""

    def __init__(self, *, agent_call_budget: int = DEFAULT_AGENT_CALLS) -> None:
        self._agent_call_budget = agent_call_budget

    @property
    def name(self) -> str:
        return "dynamic"

    async def __call__(
        self,
        model: Any,
        state: RunState,
        sandbox: Sandbox,
        max_turns: int,
    ) -> LoopResult:
        bus = state.event_bus
        # The script reads the task through its ``args`` name, and the dry run
        # must see the same input the live run will — validating against an
        # empty mapping would reject every script that reads its own input.
        args = {"task": _task_text(state)}
        home = _run_home(state)

        seal = _run_seal(state)
        source = _stored_script(home, seal)
        if source:
            # Resume re-executes the exact bytes the journal was recorded
            # against. Re-authoring would spend a model call to produce a script
            # that almost certainly words a prompt differently, and a single
            # differing call diverges the journal and strands the run.
            #
            # The pin sits in the agent's own writable workspace, so it is NOT
            # trusted just because this run wrote it: an agent holding `write` or
            # `bash` can edit it between runs. It is re-validated exactly as a
            # freshly authored script would be. The dry run costs no tokens and
            # no child agents, so there is no reason to skip it here, and
            # skipping it would make a workspace write into arbitrary
            # orchestration with the validation gate bypassed.
            rejection = await _rejection_reason(source, args)
            if rejection:
                bus.emit("dynamic.pin_rejected", {"error": rejection})
                _discard_script(home)
                return await react_loop(model, state, sandbox, max_turns)
            bus.emit("dynamic.resumed", {"bytes": len(source)})
        else:
            feedback = ""
            for attempt in range(1, _AUTHOR_ATTEMPTS + 1):
                source = await self._author(model, state, feedback, attempt)
                feedback = await _rejection_reason(source, args)
                if not feedback:
                    break
                bus.emit("dynamic.rejected", {"attempt": attempt, "error": feedback})
            else:
                bus.emit("dynamic.fallback", {"reason": feedback, "attempts": _AUTHOR_ATTEMPTS})
                return await react_loop(model, state, sandbox, max_turns)

            bus.emit("dynamic.validated", {"bytes": len(source)})
            _store_script(home, source, seal)

        outcome = await execute_script(
            source,
            host=RunHost(
                model=model,
                state=state,
                sandbox=sandbox,
                agent_call_budget=self._agent_call_budget,
                scratch_dir=home / "scratch" if home is not None else None,
            ),
            args=args,
            # Bound to the script: a journal recorded under one script can
            # never be replayed under another (seal.bound_to).
            journal=_open_journal(home, _bind(seal, source)),
            agent_call_budget=self._agent_call_budget,
        )
        return _result_from_outcome(state, outcome)

    async def _author(self, model: Any, state: RunState, feedback: str, attempt: int) -> str:
        """One forced ``emit_script`` call; empty string when nothing usable came back.

        A provider error here is treated exactly like an unusable script: the
        caller's next step is the fallback either way, so there is no second
        failure mode to distinguish.
        """
        bus = state.event_bus
        messages = [
            system_message(load_stock("arcrun", "dynamic_authoring")),
            user_message(_task_text(state)),
        ]
        if feedback:
            messages.append(
                user_message(
                    f"Your previous script was rejected: {feedback}\n"
                    "Emit a corrected script. Change only what the error names."
                )
            )

        try:
            response = await model.invoke(messages, tools=[_EMIT_SCRIPT])
        except Exception as exc:  # reason: fail-open — the ReAct fallback covers it
            bus.emit("dynamic.author.error", {"attempt": attempt, "error": str(exc)})
            return ""

        if not response.tool_calls:
            bus.emit("dynamic.author.error", {"attempt": attempt, "error": "no script emitted"})
            return ""
        source = str(response.tool_calls[0].arguments.get("source", ""))
        bus.emit(
            "dynamic.authored",
            {
                "attempt": attempt,
                "bytes": len(source),
                "reasoning": response.tool_calls[0].arguments.get("reasoning", ""),
            },
        )
        return source


def _run_seal(state: RunState) -> RunSeal | None:
    """This run's own signature directory, under the host-supplied root.

    Signature files are named for the artifact, so two runs sharing one
    directory would share ``script.py.sig`` — the second run's seal would
    silently replace the first's and strand its resume. Keying on ``run_id``
    mirrors ``_run_home`` so an artifact and its signature stay a pair.
    """
    seal = state.seal
    if seal is None:
        return None
    return replace(seal, directory=seal.directory / (state.run_id or "unpinned"))


def _bind(seal: RunSeal | None, source: str) -> RunSeal | None:
    """Narrow a seal so its signatures also commit to this exact script."""
    return None if seal is None else seal.bound_to(source.encode("utf-8"))


def _run_home(state: RunState) -> Path | None:
    """Where this run may persist, or ``None`` when it has nowhere to write.

    Keyed on ``run_id`` so a pinned run id — the task dispatcher pins one — makes
    a re-run land on the same journal and resume rather than start over.
    """
    if state.work_dir is None:
        return None
    return state.work_dir / "dynamic" / (state.run_id or "unpinned")


def _stored_script(home: Path | None, seal: RunSeal | None) -> str:
    """The script a previous attempt at this run already validated, if any.

    Stored rather than re-authored because the journal is keyed on the exact
    calls a script issues: re-authoring would reword a prompt, diverge at the
    first host call, and turn a resumable run into a dead one.

    The pin sits in the agent's own writable workspace, so it is only usable
    when the operator's seal still matches it. A rewrite is refused outright —
    :class:`SealBroken` is deliberately allowed to escape, because falling back
    to authoring would turn a detected rewrite into a silent retry and hand the
    attacker exactly the do-over they wanted.
    """
    if home is None:
        return ""
    path = home / _SCRIPT_FILENAME
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError: a single bad byte in an
        # agent-writable file must degrade to authoring, never kill the run.
        return ""
    if seal is not None:
        seal.verify_file(_SCRIPT_FILENAME, source.encode("utf-8"))
    return source


def _store_script(home: Path | None, source: str, seal: RunSeal | None) -> None:
    """Pin the validated script so a later resume runs these exact bytes.

    Best effort: losing the pin costs a re-author on resume, which is never
    worth failing a run that is otherwise ready to execute.
    """
    if home is None:
        return
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / _SCRIPT_FILENAME).write_text(source, encoding="utf-8")
        if seal is not None:
            seal.seal_file(_SCRIPT_FILENAME, source.encode("utf-8"))
    except OSError:
        _logger.warning("could not pin the dynamic script under %s", home)


def _discard_script(home: Path | None) -> None:
    """Drop a pin that no longer validates, so the next run authors afresh.

    Leaving a rejected pin in place would make every future run of this id fail
    the same way, turning one bad write into a permanently dead run id.
    """
    if home is None:
        return
    try:
        (home / _SCRIPT_FILENAME).unlink(missing_ok=True)
    except OSError:
        _logger.warning("could not discard the rejected script pin under %s", home)


def _open_journal(home: Path | None, seal: RunSeal | None) -> Journal:
    """Load this run's journal, so a re-run replays instead of repeating work.

    Corruption and tampering get opposite answers on purpose. A corrupt journal
    is a crash artefact, and running fresh is a fine response to it: it costs
    tokens and always terminates. A *tampered* one means somebody rewrote the
    record this run was about to trust, and running fresh there would turn the
    attack into an invisible retry, so it is allowed to end the run.
    """
    if home is None:
        return Journal()
    path = home / _JOURNAL_FILENAME
    try:
        return Journal.load(path, seal=seal)
    except JournalTampered:
        # Distinct from corruption on purpose: somebody rewrote the record this
        # run was about to trust, and running fresh would make that a silent
        # retry — exactly the do-over the rewrite was for.
        raise
    except JournalError:
        _logger.warning("dynamic journal at %s is unusable; running fresh", path)
        return Journal()


def _task_text(state: RunState) -> str:
    """The words the run was asked to handle."""
    return content_text(state.messages[-1].content) if state.messages else ""


async def _rejection_reason(source: str, args: dict[str, Any]) -> str:
    """Why this script cannot run, or an empty string when it can."""
    if not source.strip():
        return "no script was emitted"
    report = await dry_run(source, args=args)
    if report.ok:
        return ""
    return report.error or "the script failed its dry run"


def _result_from_outcome(state: RunState, outcome: ScriptOutcome) -> LoopResult:
    """Map a finished script onto the loop's own result shape.

    Only ``completed`` reads as success. A pause is a real state the caller has
    to act on, and a failure that reported itself as a completion would strand a
    half-finished run looking finished.
    """
    state.event_bus.emit(
        "dynamic.completed",
        {
            "status": outcome.status,
            "agent_calls": outcome.agent_calls,
            "phases": list(outcome.phases_seen),
            "resumable": outcome.is_resumable,
        },
    )
    payload: dict[str, Any] = {
        "status": _completion_status(outcome),
        "summary": _summary(outcome),
        "result": outcome.result,
    }
    if outcome.status != "completed":
        payload["error"] = outcome.kind or outcome.error or outcome.status
    state.completion_payload = payload
    return build_result(state, payload["summary"])


def _completion_status(outcome: ScriptOutcome) -> str:
    """The ``task_complete`` vocabulary for a script's ending."""
    if outcome.status == "completed":
        return "success"
    return "partial" if outcome.status == "paused" else "failed"


def _summary(outcome: ScriptOutcome) -> str:
    """One human-readable line naming what the script actually did."""
    phases = len(outcome.phases_seen)
    if outcome.status == "completed":
        return (
            f"Dynamic script completed across {phases} phase(s) "
            f"and {outcome.agent_calls} agent call(s)."
        )
    if outcome.status == "paused":
        detail = outcome.message or outcome.kind or "no reason given"
        return f"Dynamic script paused after {phases} phase(s): {detail}"
    detail = outcome.error or outcome.message or "no detail given"
    return f"Dynamic script {outcome.status} after {phases} phase(s): {detail}"
