"""Why a workflow run failed, in words an operator can act on.

The run list once showed only "Failed"; the run detail showed raw strings like
"stuck: no active run — reclaimed". These pin the plain-language reason for
every failure seen on the fleet, and which node a failed run names.
"""

from __future__ import annotations

import pytest
from arcstore.runs import NodeState
from arcstore.tasks import RUN_ENDED_UNFINISHED, SERVICE_RESTART_INTERRUPTED

from arcteam.workflow.failure import explain_node_error, run_failure_reason


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (SERVICE_RESTART_INTERRUPTED, "The Arc service restarted while this step was running."),
        (RUN_ENDED_UNFINISHED, "The step's agent stopped without marking it done or failed."),
        (
            "ArcLLMAPIError: anthropic API error (HTTP 400): prompt is too long: "
            "1503276 tokens > 1000000 maximum",
            "The step gave the AI more text than it can read at once.",
        ),
        ("timeout after 300s", "The step ran past its 300-second time limit."),
        ("ReadTimeout: ", "A service the step called did not answer in time."),
        ("WriteTimeout: ", "A service the step called did not answer in time."),
        ("loop halted: max_turns", "The step used all of its turns without finishing."),
        ("loop halted: max_cost", "The step reached its cost limit."),
        ("loop halted: runaway_loop", "The step was stopped because it kept repeating itself."),
        (
            "output does not satisfy output_schema at '<root>': ...",
            "The step's result did not match the shape the workflow expects.",
        ),
        (
            "[CREDENTIAL_MISSING] extensions: 'jira' has no stored credential; connect it",
            "A connection the step needs is not signed in.",
        ),
        ("anthropic API error (HTTP 529): overloaded", "The AI provider was busy."),
        ("cancelled", "An operator cancelled the step."),
        ("definition changed under a live run", "The workflow was edited during this run."),
        ("budget exhausted: wall clock", "The run used up its budget."),
        ("RuntimeError: something odd", "The step reported an error."),
    ],
)
def test_every_fleet_failure_has_a_plain_reason(raw: str, expected: str) -> None:
    assert explain_node_error(raw) == expected


def _failed(
    error: str, *, attempts: int = 3, finished: str = "2026-10-04T03:09:35+00:00"
) -> NodeState:
    return NodeState(
        status="failed",
        attempts=attempts,
        max_attempts=3,
        last_error=error,
        finished_at=finished,
    )


def test_failed_run_names_the_node_that_failed_first_with_its_reason() -> None:
    states = {
        "list_dropbox": NodeState(status="done", attempts=1, max_attempts=3),
        "filter_new": _failed(SERVICE_RESTART_INTERRUPTED),
        "archive": NodeState(status="cancelled", reason="upstream filter_new failed: ..."),
    }

    reason = run_failure_reason("failed", states, "node filter_new failed: x")

    assert reason is not None
    assert reason.node_id == "filter_new"
    assert reason.summary == (
        "The Arc service restarted while this step was running. Tried 3 times."
    )
    assert reason.detail == SERVICE_RESTART_INTERRUPTED


def test_the_earliest_failure_is_the_cause_when_two_nodes_failed() -> None:
    states = {
        "late": _failed("ReadTimeout: ", attempts=1, finished="2026-10-04T03:20:00+00:00"),
        "early": _failed("timeout after 60s", attempts=1, finished="2026-10-04T03:10:00+00:00"),
    }

    reason = run_failure_reason("done_with_failures", states, None)

    assert reason is not None and reason.node_id == "early"
    assert reason.summary == "The step ran past its 60-second time limit."


def test_a_run_without_node_states_falls_back_to_its_last_error() -> None:
    reason = run_failure_reason("failed", {}, "node archive failed: loop halted: max_cost")

    assert reason is not None
    assert reason.node_id == "archive"
    assert reason.summary == "The step reached its cost limit."
    assert reason.detail == "loop halted: max_cost"


def test_a_run_level_failure_names_no_node() -> None:
    reason = run_failure_reason("failed", {}, "definition changed under a live run")

    assert reason is not None and reason.node_id is None
    assert reason.summary == "The workflow was edited during this run."


@pytest.mark.parametrize("status", ["done", "running", "cancelled", "pending"])
def test_only_a_failed_run_has_a_failure_reason(status: str) -> None:
    assert run_failure_reason(status, {"x": _failed("cancelled")}, "boom") is None


def test_a_failed_run_with_nothing_recorded_still_says_so() -> None:
    reason = run_failure_reason("failed", {}, None)

    assert reason is not None and reason.node_id is None
    assert reason.summary == "The run failed without recording a reason."
