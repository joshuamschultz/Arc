"""Validation is the gate a model-authored script passes before it costs anything.

Every test here asserts the same property from a different angle: the dry run
reaches a verdict on the real interpreter without a model, a child agent, a
network call, or a file — and it always returns that verdict rather than raising
it at the caller.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcrun.dynamic.host import AgentQuotaExceeded, AgentSpec, HostFailure
from arcrun.dynamic.validate import StubHost, dry_run

COMPLETES = 'phase("research")\nlog("working")\ncomplete({"ok": 1})\n'
PAUSES = 'phase("research")\npause("verification", "a human must look")\n'
SPAWNS_THREE = (
    'results = parallel([{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}])\n'
    "complete(len(results))\n"
)
SPAWNS_FIVE = 'for i in range(5):\n    agent("go")\ncomplete(1)\n'


@pytest.mark.asyncio
async def test_a_script_that_completes_validates_clean() -> None:
    """The ordinary case: it compiles, its names resolve, it terminates."""
    report = await dry_run(COMPLETES)

    assert report.ok is True
    assert report.error == ""
    assert report.outcome is not None
    assert report.outcome.status == "completed"


@pytest.mark.asyncio
async def test_a_script_that_pauses_validates_clean_because_pausing_is_an_ending() -> None:
    """Pausing hands control back to a human; it is a plan, not a failure."""
    report = await dry_run(PAUSES)

    assert report.ok is True
    assert report.outcome is not None
    assert report.outcome.status == "paused"


@pytest.mark.asyncio
async def test_a_script_naming_something_the_host_never_defined_fails_with_that_name() -> None:
    """The report must be actionable enough for the model to repair the script."""
    report = await dry_run("x = time_now()\ncomplete(x)\n")

    assert report.ok is False
    assert "time_now" in report.error


@pytest.mark.asyncio
async def test_a_script_that_indexes_off_the_end_fails_instead_of_raising_at_the_caller() -> None:
    """A runtime script bug is a verdict, never an exception the strategy must catch."""
    report = await dry_run("items = []\ncomplete(items[3])\n")

    assert report.ok is False
    assert report.error != ""


@pytest.mark.asyncio
async def test_a_script_over_its_agent_budget_fails_and_names_the_quota() -> None:
    """Finding this in a dry run costs nothing; finding it live costs child runs.

    The error has to name the ceiling that was hit, or a model asked to repair
    the script cannot tell an over-budget fan-out from any other failure.
    """
    report = await dry_run(SPAWNS_FIVE, agent_call_budget=2)

    assert report.ok is False
    assert report.agent_calls == 2
    assert "agent" in report.error.lower()
    assert "2" in report.error


@pytest.mark.asyncio
async def test_an_infinite_loop_is_stopped_by_the_op_budget_rather_than_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model can write `while True`; validation must survive it in milliseconds."""
    monkeypatch.setattr("arcrun.dynamic.validate.DRY_RUN_MAX_OPS", 200)

    report = await dry_run('while True:\n    log("spin")\n')

    assert report.ok is False
    assert "operation" in report.error.lower()


@pytest.mark.asyncio
async def test_the_op_ceiling_the_dry_run_uses_is_the_one_the_module_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A script well inside the real ceiling must trip a lowered one, or the
    constant is decoration and the runaway test proves nothing about it."""
    monkeypatch.setattr("arcrun.dynamic.validate.DRY_RUN_MAX_OPS", 3)

    report = await dry_run('for i in range(50):\n    log("x")\ncomplete(1)\n')

    assert report.ok is False
    assert "operation" in report.error.lower()


@pytest.mark.asyncio
async def test_a_dry_run_starts_no_real_child_and_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero tokens and zero side effects is what makes the dry run affordable."""
    monkeypatch.chdir(tmp_path)

    report = await dry_run(SPAWNS_THREE)

    assert report.ok is True
    assert report.agent_calls == 3
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_phases_come_back_in_the_order_the_script_named_them() -> None:
    """The phase list is how an operator previews what a script intends to do."""
    report = await dry_run('phase("research")\nphase("write")\ncomplete(1)\n')

    assert report.phases == ("research", "write")


@pytest.mark.asyncio
async def test_a_script_reading_child_output_fields_survives_the_dry_run() -> None:
    """A stub that answered thinly would fail scripts the real host satisfies."""
    report = await dry_run('r = agent("investigate")\ncomplete(r["output"]["summary"])\n')

    assert report.ok is True


@pytest.mark.asyncio
async def test_the_stub_answers_with_an_outcome_a_script_can_read_fields_from() -> None:
    """The generic keys are the ones model-authored scripts reach for by habit."""
    host = StubHost(agent_call_budget=4)

    outcome = await host.spawn(AgentSpec(prompt="investigate", label="one"))

    assert outcome.success is True
    assert isinstance(outcome.output, dict)
    assert outcome.output["summary"]
    assert outcome.output["items"]
    assert outcome.tokens_used > 0
    assert host.agent_calls == 1


@pytest.mark.asyncio
async def test_the_stub_shapes_its_output_to_a_requested_schema() -> None:
    """A child asked for a schema returns that schema live, so it must here too."""
    host = StubHost(agent_call_budget=4)

    outcome = await host.spawn(
        AgentSpec(
            prompt="extract",
            output_schema={
                "type": "object",
                "properties": {"title": {"type": "string"}, "count": {"type": "integer"}},
            },
        )
    )

    assert set(outcome.output) == {"title", "count"}
    assert isinstance(outcome.output["title"], str)
    assert isinstance(outcome.output["count"], int)


@pytest.mark.asyncio
async def test_the_stub_answers_a_batch_in_submission_order() -> None:
    """Scripts zip outcomes back against their inputs by position."""
    host = StubHost(agent_call_budget=4)

    outcomes = await host.spawn_many(
        [AgentSpec(prompt="a", label="first"), AgentSpec(prompt="b", label="second")]
    )

    assert [outcome.output["label"] for outcome in outcomes] == ["first", "second"]
    assert host.agent_calls == 2


@pytest.mark.asyncio
async def test_the_stub_refuses_a_batch_that_would_break_the_agent_budget() -> None:
    """A stub that over-delivered would pass a script the real host refuses."""
    host = StubHost(agent_call_budget=1)

    with pytest.raises(AgentQuotaExceeded):
        await host.spawn_many([AgentSpec(prompt="a"), AgentSpec(prompt="b")])


@pytest.mark.asyncio
async def test_the_stub_refuses_a_batch_wider_than_the_host_allows() -> None:
    """``MAX_PARALLEL`` is a host-boundary limit, so the dry run must honour it."""
    host = StubHost(agent_call_budget=1_000)

    with pytest.raises(HostFailure, match="parallel"):
        await host.spawn_many([AgentSpec(prompt="x")] * 100)


def test_stub_scratch_round_trips_in_memory_and_never_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validating a script must not leave a file behind for the real run to find."""
    monkeypatch.chdir(tmp_path)
    host = StubHost(agent_call_budget=1)

    path = host.scratch_write("notes.md", "draft")

    assert host.scratch_read("notes.md") == "draft"
    assert not Path(path).exists()
    assert list(tmp_path.iterdir()) == []


def test_the_stub_reports_the_budget_it_was_given() -> None:
    """Scripts scale their own depth from `budget()`, so the stub must answer truly."""
    host = StubHost(agent_call_budget=5)

    state = host.budget()

    assert state.agent_calls_total == 5
    assert state.agent_calls_spent == 0
