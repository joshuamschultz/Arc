"""Why a workflow run failed, said in plain words.

A node's ``last_error`` is written for engineers: an exception class, a provider
body, a reclaim marker. An operator reading the run list needs one line that
says what went wrong. This module turns the raw error into that line and picks
the node a failed run should be blamed on. The raw text is kept beside it as
the technical detail; nothing here hides or rewrites the record.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from arcstore.runs import NodeState
from arcstore.tasks import RUN_ENDED_UNFINISHED, SERVICE_RESTART_INTERRUPTED

#: Run statuses that carry a failure reason.
FAILED_RUN_STATUSES = frozenset({"failed", "done_with_failures"})

_GENERIC = "The step reported an error."
_NO_REASON = "The run failed without recording a reason."

# First match wins, so the specific patterns come before the broad ones.
_PLAIN_REASONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(re.escape(SERVICE_RESTART_INTERRUPTED)),
        "The Arc service restarted while this step was running.",
    ),
    (
        re.compile(re.escape(RUN_ENDED_UNFINISHED)),
        "The step's agent stopped without marking it done or failed.",
    ),
    (
        re.compile(r"prompt is too long|context[ _]length|context window|too many tokens", re.I),
        "The step gave the AI more text than it can read at once.",
    ),
    (re.compile(r"loop halted: max_turns"), "The step used all of its turns without finishing."),
    (re.compile(r"loop halted: max_cost"), "The step reached its cost limit."),
    (re.compile(r"loop halted: max_tokens"), "The step reached its token limit."),
    (
        re.compile(r"loop halted: runaway_loop"),
        "The step was stopped because it kept repeating itself.",
    ),
    (
        re.compile(r"loop halted: error_cascade"),
        "The step was stopped after too many tool errors in a row.",
    ),
    (
        re.compile(r"output does not satisfy output_schema"),
        "The step's result did not match the shape the workflow expects.",
    ),
    (
        re.compile(r"CREDENTIAL_MISSING|no stored credential|HTTP 401|Unauthorized", re.I),
        "A connection the step needs is not signed in.",
    ),
    (
        re.compile(r"HTTP 429|HTTP 529|rate.?limit|overloaded", re.I),
        "The AI provider was busy.",
    ),
    (
        re.compile(r"\b(Read|Write|Connect|Pool)Timeout\b|\bTimeoutError\b"),
        "A service the step called did not answer in time.",
    ),
    (re.compile(r"^cancelled$"), "An operator cancelled the step."),
    (
        re.compile(r"definition changed under a live run"),
        "The workflow was edited during this run.",
    ),
    (re.compile(r"^budget exhausted"), "The run used up its budget."),
)

_STEP_TIMEOUT = re.compile(r"timeout after (\d+(?:\.\d+)?)s")
_NODE_PREFIX = re.compile(r"^node (?P<node>\S+) failed: (?P<error>.*)$", re.S)


@dataclass(frozen=True)
class FailureReason:
    """The node a failed run is blamed on, the plain reason, and the raw error."""

    node_id: str | None
    summary: str
    detail: str | None


def explain_node_error(error: str | None) -> str:
    """One plain sentence for a raw node or run error."""
    text = (error or "").strip()
    if not text:
        return _GENERIC
    timeout = _STEP_TIMEOUT.search(text)
    if timeout is not None:
        seconds = float(timeout.group(1))
        return f"The step ran past its {seconds:g}-second time limit."
    for pattern, plain in _PLAIN_REASONS:
        if pattern.search(text):
            return plain
    return _GENERIC


def run_failure_reason(
    status: str,
    node_states: Mapping[str, NodeState],
    last_error: str | None,
) -> FailureReason | None:
    """Why a failed run failed: its first failed node, else its own last error.

    ``None`` for any run that did not fail. The first node to fail is the
    cause; nodes failed or cancelled after it are consequences.
    """
    if status not in FAILED_RUN_STATUSES:
        return None
    failed = [
        (node_id, state) for node_id, state in node_states.items() if state.status == "failed"
    ]
    if failed:
        node_id, state = min(failed, key=lambda pair: pair[1].finished_at or "9999")
        return FailureReason(node_id, _with_attempts(state), state.last_error)
    if not last_error:
        return FailureReason(None, _NO_REASON, None)
    match = _NODE_PREFIX.match(last_error)
    if match is None:
        return FailureReason(None, explain_node_error(last_error), last_error)
    error = match.group("error")
    return FailureReason(match.group("node"), explain_node_error(error), error)


def _with_attempts(state: NodeState) -> str:
    plain = explain_node_error(state.last_error)
    return f"{plain} Tried {state.attempts} times." if state.attempts > 1 else plain


__all__ = ["FAILED_RUN_STATUSES", "FailureReason", "explain_node_error", "run_failure_reason"]
