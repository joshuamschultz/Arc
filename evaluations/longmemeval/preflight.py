"""Preflight (COMP-020) — refuse to spend when a measurement seam is dead.

Three gates, run in cost order, every one of them before the first question is
ingested.

*The consolidation contract* (REQ-216). ``_CONSOLIDATE_POLL_INTERVAL`` is a
module constant in ``arcagent.modules.memory.capabilities``, unreachable by
config, and the whole harness-drives-consolidation design (COMP-008) exists
because of that. If it silently drops, arcmemory's background loop starts firing
alongside the harness's own passes — and arcmemory holds no lock, so two passes
share one SQLite connection and one manifest. Nothing raises; the run just
produces corrupted consolidation state. This is an assumption guard, not a
health check, so it runs first and costs nothing.

*The environment* (REQ-196, REQ-201, REQ-215). The dataset's raw bytes are
pinned, git itself is asked whether both artifact paths are ignored, and every
question the phase will run is proven to resolve a date from either the dataset
field or the derived fallback. All three are local CPU, so they run before an
agent exists.

*The live seams* (REQ-207). Both the embedder and the distiller have degraded
silently in production, and ``configure_module_runtimes`` is FAIL-OPEN: a
``dynamics`` value pydantic rejects yields a ``NullBrain`` and the agent starts
normally. ``arc agent build --check`` tests none of this. So the last gate is a
live end-to-end pass in a scratch workspace — one real turn, one driven
consolidation — asserting object identity and observable side effects rather
than any component's self-report.

Every failure raises :class:`PreflightError` naming the assertion that failed,
because "preflight failed" without the name sends an operator back into a
five-layer config stack to find out which seam died.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from arcagent.brain import NullBrain
from arcagent.core.agent_lifecycle import activate_runtime_bindings
from arcagent.modules.memory import _runtime
from arcagent.modules.memory import capabilities as memory_capabilities

from evaluations.ingest.agent_factory import build_eval_agent
from evaluations.ingest.consolidation import ConsolidationStalledError, ConsolidationWaiter
from evaluations.ingest.driver import CONSOLIDATE_LOOP_NAME
from evaluations.longmemeval.adapter import LongMemEvalAdapter, QuestionDateError
from evaluations.longmemeval.dataset import DatasetIntegrityError, load_dataset
from evaluations.longmemeval.hygiene import REPO_ROOT, RepoHygieneError, RepoHygieneGuard

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent

REQUIRED_POLL_INTERVAL = 300.0
"""What ``capabilities._CONSOLIDATE_POLL_INTERVAL`` must still be (REQ-216)."""

REQUIRED_CONSOLIDATION_SETTINGS: Mapping[str, object] = {
    "brain": "arcmemory",
    "distill_provider": "anthropic",
    "consolidate_event_threshold": 1,
    "consolidate_idle_seconds": 0.0,
    "consolidate_interval_seconds": 0.0,
}
"""Five of the six consolidation settings, restated independently of COMP-006.

Deliberately not derived from ``agent_factory``'s override table: an assumption
guard that reads its expectations out of the thing it guards agrees with any
drift it was written to catch.
"""

REQUIRED_DYNAMICS_INTERVAL_MINUTES = 0.0
"""The sixth — arcmemory's OWN cadence gate, read back from the built brain.

Left at its 60-minute default every harness-driven pass after the first returns
an empty result and the other five settings are theatre.
"""

PREFLIGHT_QUESTION_ID = "preflight"
"""Names the scratch agent ``lme-preflight``; never a real question id."""

SEED_SESSION_KEY = "preflight"

SEED_TEXT = (
    "Preflight check: record that the scratch agent's capture path is live "
    "before the phase begins."
)
"""One turn's worth of text, fed through the real loop so the real capture hooks
fire. Capturing straight into the brain would prove the brain works and leave
the wiring — the seam that actually breaks — untested.
"""

DAILY_LOG_DIRNAME = "daily-log"


class PreflightError(RuntimeError):
    """A preflight assertion failed; the phase must not start.

    Carries the failed assertion's name so the abort message points at one seam
    instead of at the preflight as a whole.
    """

    def __init__(self, assertion: str, detail: str) -> None:
        super().__init__(f"preflight assertion {assertion!r} failed: {detail}")
        self.assertion = assertion


def _fail(assertion: str, detail: str) -> NoReturn:
    raise PreflightError(assertion, detail)


# ---------------------------------------------------------------------------
# Gate 1 — the consolidation contract (REQ-216)
# ---------------------------------------------------------------------------


def check_poll_interval_contract() -> None:
    """Assert arcmemory's background poll interval is still out of the way.

    Read through the module object rather than imported by value, so the check
    sees the constant as it is at phase start rather than as it was at import.
    """
    interval = memory_capabilities._CONSOLIDATE_POLL_INTERVAL
    if interval != REQUIRED_POLL_INTERVAL:
        _fail(
            "consolidate_poll_interval",
            f"arcagent.modules.memory.capabilities._CONSOLIDATE_POLL_INTERVAL is "
            f"{interval}, expected {REQUIRED_POLL_INTERVAL}. The harness drives every "
            "consolidation pass itself because that constant is unreachable by config; "
            "a smaller value lets the background loop fire alongside a driven pass, and "
            "arcmemory has no lock (REQ-184, REQ-216)",
        )


def check_consolidation_settings(agent: ArcAgent) -> None:
    """Assert the six consolidation settings hold, read off the LIVE agent.

    Read from the running memory state and the built brain rather than from the
    emitted TOML: a setting the config layer dropped, coerced or never folded
    into ``backend`` looks correct on disk and is absent in the object that acts
    on it.
    """
    state = _memory_state(agent)
    for name, required in REQUIRED_CONSOLIDATION_SETTINGS.items():
        actual = getattr(state.config, name, None)
        if actual != required:
            _fail(
                "consolidation_settings",
                f"[modules.memory.config] {name} is {actual!r} on the live agent, "
                f"expected {required!r}",
            )

    minutes = getattr(state.brain, "_cfg", None)
    actual_minutes = getattr(minutes, "consolidate_interval_minutes", None)
    if actual_minutes != REQUIRED_DYNAMICS_INTERVAL_MINUTES:
        _fail(
            "consolidation_settings",
            f"the built brain's consolidate_interval_minutes is {actual_minutes!r}, "
            f"expected {REQUIRED_DYNAMICS_INTERVAL_MINUTES!r}; at any larger value every "
            "harness-driven pass after the first returns an empty result",
        )


# ---------------------------------------------------------------------------
# Gate 2 — the environment (REQ-196, REQ-201, REQ-215)
# ---------------------------------------------------------------------------


def check_environment(
    *,
    dataset_path: Path,
    expected_sha256: str,
    revision: str,
    question_ids: Sequence[str],
    run_dir: Path,
    results_path: Path,
    repo_root: Path = REPO_ROOT,
) -> None:
    """Verify the dataset pin, the gitignore contract and every question's date."""
    try:
        dataset = load_dataset(dataset_path, expected_sha256=expected_sha256, revision=revision)
    except DatasetIntegrityError as exc:
        _fail("dataset_sha256", str(exc))
    except OSError as exc:
        _fail("dataset_sha256", f"the dataset could not be read: {exc}")

    try:
        RepoHygieneGuard(
            run_dir=run_dir, results_path=results_path, repo_root=repo_root
        ).check()
    except RepoHygieneError as exc:
        _fail("gitignored_artifacts", str(exc))

    for question_id in question_ids:
        try:
            LongMemEvalAdapter(dataset=dataset, question_id=question_id).question_meta()
        except QuestionDateError as exc:
            _fail(
                "question_date",
                f"question {question_id!r} cannot be anchored: {exc}. Arc's system prompt "
                "carries no date, so an unanchored question reads as a memory failure "
                "that it is not (REQ-215)",
            )


# ---------------------------------------------------------------------------
# Gate 3 — the live seams (REQ-207)
# ---------------------------------------------------------------------------


def check_live_seams(agent: ArcAgent) -> None:
    """Assert the brain and its three injected seams survived module configure.

    ``configure_module_runtimes`` is fail-open, so every one of these degrades to
    a running agent that measures something other than arcmemory.
    """
    brain = _memory_state(agent).brain
    if isinstance(brain, NullBrain):
        _fail(
            "brain_not_null",
            "the selected brain is a NullBrain: memory is a silent no-op, so the run "
            "would score retrieval against a store nothing was ever written to",
        )
    for attribute, assertion, consequence in (
        ("_embedder", "brain_embedder", "recall degrades to BM25 plus graph"),
        ("_distiller", "brain_distiller", "consolidation is a no-op and mints nothing"),
        ("_model", "brain_model", "agentic consolidation degrades to the pipeline distiller"),
    ):
        if getattr(brain, attribute, None) is None:
            _fail(
                assertion,
                f"{type(brain).__name__}.{attribute} is None after startup(): "
                f"{consequence}, and nothing raised",
            )


def check_daily_log(workspace: Path) -> None:
    """Assert the consolidation pass wrote at least one curated daily note."""
    daily_log = Path(workspace) / "memory" / DAILY_LOG_DIRNAME
    if not any(daily_log.glob("*.md")):
        _fail(
            "daily_log_written",
            f"no curated daily note under {daily_log}: consolidation reported a pass but "
            "wrote none of the files it exists to produce",
        )


async def check_consolidation_pass(agent: ArcAgent) -> None:
    """Drive one real turn plus one consolidation pass and confirm both landed."""
    workspace = Path(agent._workspace)
    await _stop_background_consolidation(agent)
    await agent.run_collected(SEED_TEXT, session_key=SEED_SESSION_KEY)

    with ConsolidationWaiter(agent=agent, workspace=workspace) as waiter:
        try:
            result = await waiter.wait()
        except ConsolidationStalledError as exc:
            _fail("consolidation_landed", str(exc))

    if not result.fired:
        _fail(
            "consolidate_poll_once_fired",
            "consolidate_poll_once() declined after a live turn, so the trigger the "
            "harness relies on never fires",
        )
    if result.window_events <= 0:
        _fail(
            "consolidation_window_events",
            "the pass consolidated a window of 0 capture events: the capture hooks ran "
            "against a brain that recorded nothing",
        )
    check_daily_log(workspace)


# ---------------------------------------------------------------------------
# The phase entry point
# ---------------------------------------------------------------------------


async def run_preflight(
    *,
    dataset_path: Path,
    expected_sha256: str,
    revision: str,
    question_ids: Sequence[str],
    run_dir: Path,
    results_path: Path,
    scratch_dir: Path,
    repo_root: Path = REPO_ROOT,
) -> None:
    """Run every gate in cost order; raise :class:`PreflightError` on the first failure.

    ``scratch_dir`` is created but never removed — COMP-019 owns workspace
    teardown, and a preflight that deleted its own evidence would leave an
    operator nothing to read after a seam failure.
    """
    check_poll_interval_contract()
    check_environment(
        dataset_path=dataset_path,
        expected_sha256=expected_sha256,
        revision=revision,
        question_ids=question_ids,
        run_dir=run_dir,
        results_path=results_path,
        repo_root=repo_root,
    )

    scratch_dir.mkdir(parents=True, exist_ok=True)
    agent = await build_eval_agent(question_id=PREFLIGHT_QUESTION_ID, run_dir=scratch_dir)
    try:
        check_consolidation_settings(agent)
        check_live_seams(agent)
        await check_consolidation_pass(agent)
    finally:
        await agent.shutdown()


def _memory_state(agent: ArcAgent) -> _runtime._State:
    """Resolve the running agent's memory state, or fail the preflight.

    The preflight runs in its own task, and memory's ``state()`` fails closed on
    an unbound DID, so the binding is activated here exactly as COMP-008 does.
    """
    activate_runtime_bindings(agent)
    try:
        return _runtime.state()
    except RuntimeError as exc:
        _fail(
            "memory_module_configured",
            f"the memory module has no runtime state on the scratch agent: {exc}",
        )


async def _stop_background_consolidation(agent: ArcAgent) -> None:
    """Cancel arcmemory's polling loop before the preflight drives its own pass.

    ``startup()`` spawns it, and REQ-184 is the same invariant here as it is in
    ingest: a poll landing inside a driven pass shares one manifest with it.
    """
    registry = agent._capability_registry
    if registry is None:
        _fail(
            "background_loop_stopped",
            f"the scratch agent has no capability registry, so {CONSOLIDATE_LOOP_NAME!r} "
            "cannot be proven stopped",
        )
    await registry.unregister("background_task", CONSOLIDATE_LOOP_NAME)
    if await registry.get_task(CONSOLIDATE_LOOP_NAME) is not None:
        _fail(
            "background_loop_stopped",
            f"{CONSOLIDATE_LOOP_NAME!r} is still registered after unregister; a background "
            "pass could interleave with the driven one and arcmemory has no lock (REQ-184)",
        )


__all__ = [
    "DAILY_LOG_DIRNAME",
    "PREFLIGHT_QUESTION_ID",
    "REQUIRED_CONSOLIDATION_SETTINGS",
    "REQUIRED_DYNAMICS_INTERVAL_MINUTES",
    "REQUIRED_POLL_INTERVAL",
    "SEED_SESSION_KEY",
    "SEED_TEXT",
    "PreflightError",
    "check_consolidation_pass",
    "check_consolidation_settings",
    "check_daily_log",
    "check_environment",
    "check_live_seams",
    "check_poll_interval_contract",
    "run_preflight",
]
