"""Resume equivalence under a real SIGKILL — T-823 (COMP-018, COMP-019, COMP-020).

Covers REQ-204 (the question is the atomic unit of resume) and REQ-205 (a
workspace without ``.ingest_complete`` is deleted and rebuilt, never continued).

*The crash is real, because the bug this guards against only appears in a real
one.* The harness runs in a child process and is stopped with
``os.kill(pid, signal.SIGKILL)`` — not an injected exception, which unwinds
``finally`` blocks and lets teardown tidy the very partial state under test, and
not ``SIGTERM``, which the process could catch and clean up after. SIGKILL cannot
be caught, so what the resume finds on disk is exactly what a killed run leaves.

*The kill is placed, not raced.* The stub agent blocks forever on the second of
three ingest turns of one question, so the child is provably parked inside
``IngestDriver.ingest`` — after one chunk landed, before ``mark_ingest_complete``
— and stays there until the signal arrives. The parent waits for the child's
sentinel before signalling, so the kill can land neither before ingest started
nor after it finished.

*Everything under test is production code.* The ledger, the workspace lifecycle,
the budget governor, the chunker, the fidelity gate, the ingest driver, the
scrubber, the adapter and the manifest are the real classes; only the seams
needing a network, a provider key or a live arcmemory binding are stubbed. No
provider is called, no dataset file is read, and nothing is written outside
``tmp_path``.

This file is also the child entrypoint: ``python test_resume_equivalence.py
<workdir> <label> <kill_at>`` runs one harness phase, so the stubs the test
asserts against and the stubs the child runs are the same code.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from evaluations.longmemeval.ingest.lifecycle import INGEST_COMPLETE_MARKER
from evaluations.longmemeval.ingest.types import Chunk
from evaluations.longmemeval.adapter import QuestionMeta
from evaluations.longmemeval.budget import (
    CURRENT_PRICING_TABLE_VERSION,
    BudgetGovernor,
    Estimate,
)
from evaluations.longmemeval.dataset import Dataset
from evaluations.longmemeval.ledger import ResultLedger
from evaluations.longmemeval.manifest import MeasurementScope, Provenance, RunManifest
from evaluations.longmemeval.query import Answer
from evaluations.longmemeval.runner import PhaseRunner

REPO_ROOT = Path(__file__).resolve().parents[3]

N_QUESTIONS = 5
SESSIONS_PER_QUESTION = 3
KILL_QUESTION = "q3"

KILL_SESSION_KEY = "ingest:1:0"
"""The second of the question's three ingest turns.

Chosen so the kill is unambiguously *mid*-ingest: session 0 has landed, session 2
has not been fed, and the marker that would make the workspace resumable is two
sessions away from being written.
"""

SENTINEL_NAME = "at_kill_point"
EVENTS_NAME = "events.jsonl"
ROWS_RELPATH = ("results", "rows.jsonl")

SENTINEL_TIMEOUT = 60.0
"""Seconds to wait for the child to reach the kill point before failing the test.

A child that never gets there is a harness defect, not a slow machine: the whole
run is CPU-local with no provider call in it.
"""

CHILD_TIMEOUT = 120.0
"""Seconds an uninterrupted child gets before it is killed and the test fails.

Every subprocess in this file is bounded, so a harness that wedges fails the
suite instead of hanging it.
"""


# ---------------------------------------------------------------------------
# The dataset the child runs (built identically in parent and child)
# ---------------------------------------------------------------------------


def _question(question_id: str) -> dict[str, Any]:
    """One dataset entry with three single-turn sessions, so ingest has a middle."""
    session_ids = [f"{question_id}_s{index}" for index in range(SESSIONS_PER_QUESTION)]
    texts = ["I passed the PMP exam.", "I started a new job.", "I moved to Denver."]
    return {
        "question_id": question_id,
        "question_type": "single-session-user",
        "question": "When did I pass the PMP exam?",
        "answer": "May 2023",
        "question_date": "2023/06/01 (Thu) 09:00",
        "answer_session_ids": session_ids[:1],
        "haystack_dates": [f"2023/05/{20 + index:02d} (Sat) 02:21" for index in range(3)],
        "haystack_session_ids": session_ids,
        "haystack_sessions": [
            [{"role": "user", "content": texts[index], "has_answer": index == 0}]
            for index in range(SESSIONS_PER_QUESTION)
        ],
    }


def _dataset() -> Dataset:
    questions = [_question(f"q{index}") for index in range(1, N_QUESTIONS + 1)]
    return Dataset(questions=questions, sha256="0" * 64, revision="test")


def _manifest() -> RunManifest:
    return RunManifest(
        provenance=Provenance(
            git_sha="deadbeef",
            git_dirty=False,
            harness_version="0.1.0",
            config_hash="c" * 64,
            dataset_sha256="0" * 64,
            dataset_revision="test",
            agent_model_id="anthropic/claude-sonnet-4-5-20250929",
            judge_model_id="openai/gpt-4o-2024-08-06",
            embedder_model_id="all-MiniLM-L6-v2",
            distiller_model_id="anthropic/claude-sonnet-4-5-20250929",
            tier="personal",
            run_timestamp_utc="2026-07-30T00:00:00+00:00",
        ),
        measurement_scope=MeasurementScope(workpad_enabled=True, policy_enabled=True),
        pricing_table_version=CURRENT_PRICING_TABLE_VERSION,
    )


def _estimate() -> Estimate:
    return Estimate(
        n_calls=10,
        tokens_in=1000,
        tokens_out=100,
        cost_usd=1000.0,
        pricing_table_version=CURRENT_PRICING_TABLE_VERSION,
        n_questions=N_QUESTIONS,
        n_sessions=N_QUESTIONS * SESSIONS_PER_QUESTION,
        n_chunks=N_QUESTIONS * SESSIONS_PER_QUESTION,
        n_voided_questions=0,
    )


# ---------------------------------------------------------------------------
# The stubbed seams, plus the placed kill point
# ---------------------------------------------------------------------------


class _Result:
    """What arcrun's ``RunResult`` gives the meter, and the judge its label."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.turns = 1
        self.cost_usd = 0.01
        self.tokens_used: dict[str, int] = {"input": 100, "output": 20, "total": 120}


class _Registry:
    """A capability registry reporting the consolidation loop already gone."""

    async def unregister(self, kind: str, name: str) -> None:
        return None

    async def get_task(self, name: str) -> Any:
        return None


class _Recorder:
    """Append-only event log the parent reads back after the child is killed.

    Flushed per event and never re-read by the child, so a SIGKILL costs at most
    the event in flight — the same durability shape the ledger under test has.
    """

    def __init__(self, path: Path, label: str) -> None:
        self._path = path
        self._label = label

    def write(self, kind: str, **fields: Any) -> None:
        payload: dict[str, Any] = {"run": self._label, "kind": kind, **fields}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()


class _StubHarness:
    """Every seam the runner injects, wired to record rather than to call a provider."""

    def __init__(self, workdir: Path, recorder: _Recorder, kill_at: str | None) -> None:
        self._workdir = workdir
        self._recorder = recorder
        self._kill_at = kill_at

    async def build_agent(self, *, question_id: str, run_dir: Path) -> _StubAgent:
        # Recorded at build time, which is *after* WorkspaceLifecycle has decided
        # whether to resume or rebuild. An empty listing here on a question whose
        # partial workspace survived a crash is the proof that it was destroyed.
        self._recorder.write(
            "build-agent",
            question_id=question_id,
            pre_existing=sorted(path.name for path in run_dir.iterdir()),
        )
        return _StubAgent(self, question_id, run_dir)

    async def build_judge(self, *, run_dir: Path) -> _StubAgent:
        return _StubAgent(self, f"judge:{run_dir.name}", run_dir, judge=True)

    def build_waiter(self, *, agent: Any, workspace: Path) -> _StubWaiter:
        return _StubWaiter()

    async def preflight(self, *, question_ids: Sequence[str]) -> None:
        self._recorder.write("preflight", question_ids=list(question_ids))

    async def ask(self, agent: Any, meta: QuestionMeta, chunks: Sequence[Chunk]) -> Answer:
        result = await agent.run_collected(meta.question, session_key="query")
        return Answer(
            text=result.content,
            question_date_used=meta.question_date,
            question_date_source=meta.question_date_source,
            recalled_chunk_ids=[f"{chunk.session_idx}:{chunk.chunk_idx}" for chunk in chunks[:1]],
        )

    async def turn(self, agent: _StubAgent, session_key: str) -> None:
        """Record the turn, park forever at the kill point, then land the chunk."""
        self._recorder.write("turn", question_id=agent.label, session_key=session_key)

        if agent.label == self._kill_at and session_key == KILL_SESSION_KEY:
            self._recorder.write("kill-point", question_id=agent.label, session_key=session_key)
            (self._workdir / SENTINEL_NAME).touch()
            # Never returns. The parent SIGKILLs the process here, so the crash
            # lands inside ingest by construction rather than by timing luck —
            # and the chunk in flight is left unlanded, as a real crash leaves it.
            await asyncio.Event().wait()

        if session_key.startswith("ingest:"):
            fed = agent.run_dir / "fed"
            fed.mkdir(parents=True, exist_ok=True)
            (fed / session_key.replace(":", "_")).touch()


class _StubAgent:
    """An agent whose only side effect is a per-chunk artifact in its workspace.

    The artifacts stand in for the haystack a real ingest leaves on disk: after
    the crash they are what a buggy resume would find and mistake for a finished
    question.
    """

    def __init__(
        self, harness: _StubHarness, label: str, run_dir: Path, *, judge: bool = False
    ) -> None:
        self._harness = harness
        self.label = label
        self.run_dir = run_dir
        self._judge = judge
        self._capability_registry = _Registry()
        self._workspace = run_dir / "workspace"

    async def run_collected(self, input_text: str, *, session_key: str) -> _Result:
        await self._harness.turn(self, session_key)
        return _Result("yes" if self._judge else "May 2023")

    async def shutdown(self) -> None:
        return None


class _StubWaiter:
    """The consolidation waiter's shape, without an arcmemory runtime binding."""

    @property
    def warnings(self) -> Sequence[str]:
        return ()

    def __enter__(self) -> _StubWaiter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def wait(self) -> Any:
        return None


# ---------------------------------------------------------------------------
# Child entrypoint
# ---------------------------------------------------------------------------


async def _run_phase(workdir: Path, label: str, kill_at: str | None) -> None:
    """Run one oracle phase over the stub dataset, writing only under ``workdir``."""
    recorder = _Recorder(workdir / EVENTS_NAME, label)
    harness = _StubHarness(workdir, recorder, kill_at)
    results_dir = workdir / "results"

    with ResultLedger(results_dir / "rows.jsonl") as ledger:
        runner = PhaseRunner(
            dataset=_dataset(),
            manifest=_manifest(),
            ledger=ledger,
            budget=BudgetGovernor(estimate=_estimate(), ledger_path=results_dir / "spend.jsonl"),
            runs_root=workdir / "runs",
            results_dir=results_dir,
            preflight=harness.preflight,
            agent_builder=harness.build_agent,
            judge_builder=harness.build_judge,
            waiter_builder=harness.build_waiter,
            asker=harness,
        )
        report = await runner.run("oracle")

    recorder.write(
        "phase-done",
        requested=report.requested,
        skipped=report.skipped,
        complete=report.complete,
        void=report.void,
        error=report.error,
    )


def _child_main(argv: Sequence[str]) -> int:
    workdir, label, kill_at = Path(argv[0]), argv[1], argv[2]
    asyncio.run(_run_phase(workdir, label, kill_at or None))
    return 0


# ---------------------------------------------------------------------------
# Parent-side process control
# ---------------------------------------------------------------------------


def _spawn(workdir: Path, label: str, kill_at: str = "") -> subprocess.Popen[str]:
    """Start a child harness run. ``PYTHONPATH`` is what makes ``evaluations`` importable."""
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), str(workdir), label, kill_at],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish(proc: subprocess.Popen[str]) -> None:
    """Wait for a child that is supposed to exit cleanly, bounded and asserted."""
    try:
        _, stderr = proc.communicate(timeout=CHILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(f"child harness did not finish within {CHILD_TIMEOUT}s")
    assert proc.returncode == 0, f"child harness failed:\n{stderr}"


def _run_uninterrupted(workdir: Path, label: str) -> None:
    _finish(_spawn(workdir, label))


def _sigkill_mid_ingest(workdir: Path, label: str) -> None:
    """Run a child until it parks inside ingest, then SIGKILL it there."""
    proc = _spawn(workdir, label, kill_at=KILL_QUESTION)
    sentinel = workdir / SENTINEL_NAME
    deadline = time.monotonic() + SENTINEL_TIMEOUT

    while not sentinel.exists():
        if proc.poll() is not None:
            _, stderr = proc.communicate()
            pytest.fail(f"child exited ({proc.returncode}) before reaching ingest:\n{stderr}")
        if time.monotonic() > deadline:
            proc.kill()
            proc.communicate()
            pytest.fail(f"child never reached the kill point within {SENTINEL_TIMEOUT}s")
        time.sleep(0.01)

    os.kill(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=CHILD_TIMEOUT)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL is not refusable
        pytest.fail("the child survived SIGKILL")
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()

    assert proc.returncode == -signal.SIGKILL, (
        f"the child exited with {proc.returncode}, so it was not killed by an "
        "uncatchable signal and may have run teardown on the way out"
    )


# ---------------------------------------------------------------------------
# Parent-side readers
# ---------------------------------------------------------------------------


def _rows_path(workdir: Path) -> Path:
    return workdir.joinpath(*ROWS_RELPATH)


def _lines(workdir: Path) -> list[str]:
    return _rows_path(workdir).read_text(encoding="utf-8", errors="replace").splitlines()


def _rows(workdir: Path) -> list[dict[str, Any]]:
    """Every parseable row. Unparseable lines are the crash signature, not an error."""
    rows: list[dict[str, Any]] = []
    for line in _lines(workdir):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _complete(workdir: Path) -> set[str]:
    return {row["question_id"] for row in _rows(workdir) if row["status"] == "complete"}


def _events(workdir: Path, label: str) -> list[dict[str, Any]]:
    text = (workdir / EVENTS_NAME).read_text(encoding="utf-8", errors="replace")
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event["run"] == label:
            events.append(event)
    return events


def _ingest_keys(workdir: Path, label: str, question_id: str) -> list[str]:
    return [
        event["session_key"]
        for event in _events(workdir, label)
        if event["kind"] == "turn"
        and event["question_id"] == question_id
        and event["session_key"].startswith("ingest:")
    ]


def _phase_done(workdir: Path, label: str) -> dict[str, Any]:
    (report,) = [event for event in _events(workdir, label) if event["kind"] == "phase-done"]
    return report


# ---------------------------------------------------------------------------
# REQ-204 / REQ-205 — the crash, the resume, and what must survive it
# ---------------------------------------------------------------------------


def test_the_kill_lands_inside_ingest_and_leaves_an_unmarked_workspace(tmp_path: Path) -> None:
    """The precondition every other assertion here rests on.

    A kill landing before the question started, or after its marker was written,
    would exercise a resume path that has nothing partial to recover from — and
    would still pass the equivalence check below. So the placement is asserted
    directly rather than assumed.
    """
    workdir = tmp_path / "crashed"
    workdir.mkdir()
    _sigkill_mid_ingest(workdir, "run1")

    assert _ingest_keys(workdir, "run1", KILL_QUESTION) == ["ingest:0:0", KILL_SESSION_KEY], (
        "the child was not parked in the middle of the question's ingest"
    )
    run_dir = workdir / "runs" / KILL_QUESTION
    assert run_dir.is_dir(), "the partial workspace did not survive the kill"
    assert not (run_dir / INGEST_COMPLETE_MARKER).exists(), (
        "the marker exists, so ingest had already finished when the signal landed"
    )
    assert sorted(path.name for path in (run_dir / "fed").iterdir()) == ["ingest_0_0"], (
        "the surviving haystack is not the partial prefix a mid-ingest crash leaves"
    )
    assert _complete(workdir) == {"q1", "q2"}, "the ledger does not stop where the crash did"
    assert not (workdir / "runs" / "q2").exists(), "a completed question's workspace leaked"


def test_resume_after_sigkill_matches_an_uninterrupted_run(tmp_path: Path) -> None:
    """REQ-204: the completed set after crash-and-resume equals the clean run's."""
    control = tmp_path / "control"
    control.mkdir()
    _run_uninterrupted(control, "control")

    workdir = tmp_path / "crashed"
    workdir.mkdir()
    _sigkill_mid_ingest(workdir, "run1")
    _run_uninterrupted(workdir, "run2")

    expected = {f"q{index}" for index in range(1, N_QUESTIONS + 1)}
    assert _complete(control) == expected
    assert _complete(workdir) == _complete(control)
    assert [row["question_id"] for row in _rows(workdir)] == [
        row["question_id"] for row in _rows(control)
    ], "resume re-ran or dropped a question the uninterrupted run did not"
    assert {row["question_id"]: row["answer"] for row in _rows(workdir)} == {
        row["question_id"]: row["answer"] for row in _rows(control)
    }

    resumed = _phase_done(workdir, "run2")
    assert (resumed["skipped"], resumed["complete"]) == (2, 3), (
        "the ledger done-set did not carry the finished questions across the crash"
    )
    assert _ingest_keys(workdir, "run2", "q1") == [], "a completed question was ingested twice"
    assert not (workdir / "runs").exists() or list((workdir / "runs").iterdir()) == [], (
        "teardown left a workspace behind after the resumed phase"
    )


def test_the_partial_workspace_is_deleted_and_rebuilt_not_resumed(tmp_path: Path) -> None:
    """REQ-205: the classic resume bug — workspace-exists read as question-complete.

    A resume that continued into the surviving directory would feed only the
    chunk it stopped on, leaving a haystack with a hole in it and scoring
    arcmemory for the harness's damage. The tripwire proves the tree was
    destroyed; the ingest keys prove the question was fed from chunk zero again.
    """
    workdir = tmp_path / "crashed"
    workdir.mkdir()
    _sigkill_mid_ingest(workdir, "run1")

    tripwire = workdir / "runs" / KILL_QUESTION / "TRIPWIRE"
    tripwire.touch()

    _run_uninterrupted(workdir, "run2")

    (built,) = [
        event
        for event in _events(workdir, "run2")
        if event["kind"] == "build-agent" and event["question_id"] == KILL_QUESTION
    ]
    assert built["pre_existing"] == [], (
        f"the crashed workspace was reused, not rebuilt: it still held {built['pre_existing']}"
    )
    assert _ingest_keys(workdir, "run2", KILL_QUESTION) == [
        "ingest:0:0",
        "ingest:1:0",
        "ingest:2:0",
    ], "the rebuilt workspace was not fed the whole haystack from the start"
    assert not tripwire.exists()
    assert KILL_QUESTION in _complete(workdir)


def test_a_truncated_final_ledger_line_loads_and_its_question_is_rebuilt(
    tmp_path: Path,
) -> None:
    """REQ-204: a half-written row is the crash signature the ledger tolerates.

    The kill is real, but the truncation is applied here rather than produced by
    it: ``ResultLedger.append`` flushes every row, so bytes reaching the page
    cache outlive the process and a *process*-level SIGKILL cannot leave a
    partial line. The line in flight is lost at the layer below — a partial
    ``write`` or a machine-level crash — which this reproduces by truncating the
    final row mid-JSON at a byte boundary, exactly the shape ``_scan`` documents.
    """
    control = tmp_path / "control"
    control.mkdir()
    _run_uninterrupted(control, "control")

    workdir = tmp_path / "crashed"
    workdir.mkdir()
    _sigkill_mid_ingest(workdir, "run1")
    fragment = _truncate_final_row(_rows_path(workdir))

    # The load itself is the assertion: the resume must not die decoding the
    # crash it exists to recover from, and the half-row must count as not done.
    with ResultLedger(_rows_path(workdir)) as ledger:
        assert ledger.done_set() == {"q1"}

    _run_uninterrupted(workdir, "run2")

    lines = _lines(workdir)
    unparseable = [line for line in lines if _is_unparseable(line)]
    assert unparseable == [fragment.decode("utf-8")], (
        "the half-row fused with the row appended after it, losing both questions"
    )
    assert [row["question_id"] for row in _rows(workdir)] == ["q1", "q2", "q3", "q4", "q5"]
    assert _complete(workdir) == _complete(control)
    assert _phase_done(workdir, "run2")["skipped"] == 1, "the truncated row was counted as done"
    assert _ingest_keys(workdir, "run2", "q2") == ["ingest:0:0", "ingest:1:0", "ingest:2:0"], (
        "the question behind the truncated row was scored again instead of rebuilt"
    )


def _truncate_final_row(path: Path) -> bytes:
    """Cut the file's last row in half, leaving the file ending mid-JSON."""
    data = path.read_bytes()
    assert data.endswith(b"\n"), "the crashed ledger did not end on a row boundary"
    start = data.rindex(b"\n", 0, len(data) - 1) + 1
    keep = start + (len(data) - 1 - start) // 2
    assert start < keep < len(data) - 1, "the truncation point is not inside the final row"
    with path.open("r+b") as handle:
        handle.truncate(keep)
    return data[start:keep]


def _is_unparseable(line: str) -> bool:
    if not line.strip():
        return False
    try:
        json.loads(line)
    except json.JSONDecodeError:
        return True
    return False


if __name__ == "__main__":
    sys.exit(_child_main(sys.argv[1:]))
