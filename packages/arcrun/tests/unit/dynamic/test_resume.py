"""Resume is the reason the journal exists, so it is tested end to end.

A journal that is written but never read back would be a feature on paper only.
These tests go through the real path: a run with a durable home records what it
did, and a second run over the same home replays instead of paying for the work
again.
"""

from __future__ import annotations

from pathlib import Path

from arcrun.dynamic.host import AgentOutcome, AgentSpec, BudgetState, ScriptOutcome
from arcrun.dynamic.interpreter import execute_script
from arcrun.dynamic.journal import Journal

SCRIPT = (
    'phase("work")\n'
    'jobs = [{"prompt": "a", "label": "a"}, {"prompt": "b", "label": "b"}]\n'
    "results = parallel(jobs)\n"
    'second = agent("summarise", {"label": "summary"})\n'
    'complete({"ids": [r["agent_id"] for r in results], "summary": second["agent_id"]})\n'
)


class CountingHost:
    """Counts real work so a replayed run can be told from a repeated one."""

    def __init__(self, *, stop_after: int | None = None) -> None:
        self.spawned = 0
        self._stop_after = stop_after

    async def spawn(self, spec: AgentSpec) -> AgentOutcome:
        if self._stop_after is not None and self.spawned >= self._stop_after:
            raise _Stop
        self.spawned += 1
        return AgentOutcome(
            agent_id=f"agent-{self.spawned}", success=True, output={"n": self.spawned}
        )

    async def spawn_many(self, specs: list[AgentSpec]) -> list[AgentOutcome]:
        return [await self.spawn(spec) for spec in specs]

    def phase(self, title: str) -> None:
        return None

    def log(self, message: str) -> None:
        return None

    def budget(self) -> BudgetState:
        return BudgetState()

    def scratch_write(self, name: str, content: str) -> str:
        return name

    def scratch_read(self, name: str) -> str:
        return ""


class _Stop(Exception):  # noqa: N818 — stands in for a crash, not an error type
    """Stands in for the process dying part-way through a run."""


async def test_a_second_run_over_the_same_journal_does_no_work_again(
    tmp_path: Path,
) -> None:
    """The whole point: replay returns recorded results and spawns nothing."""
    path = tmp_path / "journal.jsonl"

    first_host = CountingHost()
    first = await execute_script(SCRIPT, host=first_host, journal=Journal.load(path))
    assert first.status == "completed"
    assert first_host.spawned == 3

    second_host = CountingHost()
    second = await execute_script(SCRIPT, host=second_host, journal=Journal.load(path))
    assert second.status == "completed"
    assert second_host.spawned == 0, "a replayed run must not re-spawn a single agent"
    assert second.result == first.result


async def test_a_run_that_died_part_way_resumes_where_it_stopped(
    tmp_path: Path,
) -> None:
    """Only the work never recorded is paid for again."""
    path = tmp_path / "journal.jsonl"

    crashed = CountingHost(stop_after=2)
    try:
        await execute_script(SCRIPT, host=crashed, journal=Journal.load(path))
    except _Stop:
        pass
    assert crashed.spawned == 2

    resumed = CountingHost()
    outcome: ScriptOutcome = await execute_script(SCRIPT, host=resumed, journal=Journal.load(path))
    assert outcome.status == "completed"
    assert resumed.spawned == 1, "only the one unrecorded agent should run again"
    assert outcome.result["ids"] == ["agent-1", "agent-2"]


async def test_editing_the_script_between_runs_is_caught_not_silently_replayed(
    tmp_path: Path,
) -> None:
    """A changed call at a recorded position means the journal no longer applies."""
    path = tmp_path / "journal.jsonl"
    await execute_script(SCRIPT, host=CountingHost(), journal=Journal.load(path))

    edited = SCRIPT.replace('"prompt": "a"', '"prompt": "something else"')
    outcome = await execute_script(edited, host=CountingHost(), journal=Journal.load(path))
    assert outcome.status == "failed"
    assert "diverg" in outcome.error.lower() or "nondeterministic" in outcome.error.lower()


async def test_a_run_with_no_durable_home_still_works_it_just_cannot_resume(
    tmp_path: Path,
) -> None:
    """``work_dir`` is optional, so an in-memory run must behave identically."""
    host = CountingHost()
    outcome = await execute_script(SCRIPT, host=host, journal=Journal())
    assert outcome.status == "completed"
    assert host.spawned == 3
    assert not list(tmp_path.iterdir())
