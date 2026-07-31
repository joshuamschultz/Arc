"""Preflight tests — COMP-020 / REQ-196, REQ-201, REQ-207, REQ-215, REQ-216.

A preflight that cannot fail is worthless, so most of this file is about the
failures. Every degrade the preflight exists to catch is built here for real —
an emptied ``distill_provider``, an ``embed_backend = "none"``, a
``brain = "none"``, a drifted consolidation setting, a lowered poll interval —
and each is put through the same code path a phase runs.

*The agents are real and the path is the production one.* The degraded agents are
built by rendering COMP-006's own config with ``write_eval_agent_config`` and
rewriting exactly the one line under test, then constructing ``ArcAgent`` the way
``build_eval_agent`` does. That is what makes these tests evidence: a stub brain
with ``_distiller = None`` would prove only that ``getattr`` works, whereas an
agent built from an emptied ``distill_provider`` proves the config reaches the
seam — and that module configure starts the agent anyway, without raising.

*No network, no LLM call, nothing written outside ``tmp_path``.* Startup wires the
arcllm seams without invoking them, so every assertion here lands before the first
LLM call. One function cannot be exercised offline and is called out where it
lives: :func:`check_consolidation_pass` drives a real turn and a real
distillation, so only its pure half — the curated daily-note check — is covered
here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from arcstore.config import ENV_DATA_DIR

from evaluations.ingest.agent_factory import pin_arcstore_data_dir, write_eval_agent_config
from evaluations.ingest.driver import CONSOLIDATE_LOOP_NAME
from evaluations.longmemeval.preflight import (
    REQUIRED_POLL_INTERVAL,
    PreflightError,
    _stop_background_consolidation,
    check_daily_log,
    check_environment,
    check_live_seams,
    check_memory_settings,
    check_poll_interval_contract,
    run_preflight,
)

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent

GIT = shutil.which("git")
requires_git = pytest.mark.skipif(GIT is None, reason="git is required for the hygiene gate")

_FIXTURES = Path(__file__).parent / "fixtures"
_ORACLE_FIXTURE = _FIXTURES / "lme_oracle_fixture.json"
_QUESTION_ID = "fixture_oracle_temporal_reasoning_1"
_REVISION = "1d0f1a5c9e3b47a28f6c05d9b7e4a3128c9f6b02"

# The two rules COMP-014 checks at phase start, as `.gitignore` ships them.
_RUN_DIR_PATTERN = "/runs/"
_RESULTS_PATTERN = "/results/"

# The exact config lines a degraded agent rewrites. Asserted present before the
# rewrite, so a template edit fails loudly here instead of silently building an
# undegraded agent that passes the test it was written to fail.
_DISTILLER_LINE = 'distill_provider = "anthropic"'
_EMBEDDER_LINE = 'embed_backend = "local"'
_BRAIN_LINE = 'brain = "arcmemory"'
_EVENT_THRESHOLD_LINE = "consolidate_event_threshold = 1"
_DYNAMICS_LINE = "consolidate_interval_minutes = 0.0"
_MAX_EVENT_CHARS_LINE = "max_event_chars = 6000"
_BUDGET_LINE = "budget = 34000"

AgentFactory = Callable[..., Awaitable["ArcAgent"]]


# ---------------------------------------------------------------------------
# Real agents, built offline
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME, the Arc config root and arcstore at throwaway directories.

    The developer's `~/.arc/arcagent.toml` merges under every per-agent config,
    so an unisolated run would build an agent from settings the harness never
    wrote — and `ARCSTORE_DATA_DIR` outranks the emitted `[arcstore] data_dir`,
    so it is pinned here for `monkeypatch` to restore afterwards.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-config"))
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "arcstore-placeholder"))
    return home


async def _build_agent(
    run_dir: Path,
    *,
    question_id: str,
    edits: tuple[tuple[str, str], ...] = (),
) -> ArcAgent:
    """Build and start one real eval agent, optionally degrading its config.

    With no edits this is byte-for-byte what `build_eval_agent` produces; the
    edits exist so a degrade is introduced at the only place it ever occurs in
    production — one value in the emitted TOML.
    """
    from arcagent.core.agent import ArcAgent
    from arcagent.core.config import load_config

    pin_arcstore_data_dir(run_dir)
    config_path = write_eval_agent_config(question_id=question_id, run_dir=run_dir)
    if edits:
        text = config_path.read_text(encoding="utf-8")
        for old, new in edits:
            assert old in text, f"{old!r} is no longer in the rendered eval config"
            text = text.replace(old, new, 1)
        config_path.write_text(text, encoding="utf-8")

    agent = ArcAgent(load_config(config_path), config_path=config_path)
    await agent.startup()
    return agent


@pytest.fixture
async def eval_agent(tmp_path: Path, isolated_arc_home: Path) -> AsyncIterator[ArcAgent]:
    """The undegraded eval agent, exactly as a phase builds it."""
    run_dir = (tmp_path / "runs" / "healthy").resolve()
    run_dir.mkdir(parents=True)
    agent = await _build_agent(run_dir, question_id="healthy")
    try:
        yield agent
    finally:
        await agent.shutdown()


@pytest.fixture
async def degraded_agent(
    tmp_path: Path, isolated_arc_home: Path
) -> AsyncIterator[AgentFactory]:
    """Build one deliberately degraded agent per test; shut every one of them down."""
    built: list[ArcAgent] = []

    async def build(name: str, *edits: tuple[str, str]) -> ArcAgent:
        run_dir = (tmp_path / "runs" / name).resolve()
        run_dir.mkdir(parents=True)
        agent = await _build_agent(run_dir, question_id=name, edits=edits)
        built.append(agent)
        return agent

    try:
        yield build
    finally:
        for agent in built:
            await agent.shutdown()


# ---------------------------------------------------------------------------
# T-825 — the consolidation contract (REQ-216)
# ---------------------------------------------------------------------------


def test_poll_interval_contract_holds_today() -> None:
    """The guard is only meaningful while the real constant still satisfies it."""
    check_poll_interval_contract()


def test_lowered_poll_interval_fails_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """A smaller interval lets the background loop race the harness's own passes."""
    from arcagent.modules.memory import capabilities

    monkeypatch.setattr(capabilities, "_CONSOLIDATE_POLL_INTERVAL", 5.0)

    with pytest.raises(PreflightError) as excinfo:
        check_poll_interval_contract()

    assert excinfo.value.assertion == "consolidate_poll_interval"
    assert "5.0" in str(excinfo.value)
    assert str(REQUIRED_POLL_INTERVAL) in str(excinfo.value)


@requires_git
async def test_lowered_poll_interval_aborts_before_any_ingest(
    environment: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract is checked first, so nothing is built and nothing is spent.

    Every other argument is valid here, so the run aborts on the constant alone —
    and the scratch workspace the live gate would need is the proof it never got
    that far.
    """
    from arcagent.modules.memory import capabilities

    monkeypatch.setattr(capabilities, "_CONSOLIDATE_POLL_INTERVAL", 5.0)
    scratch_dir = tmp_path / "scratch"

    with pytest.raises(PreflightError) as excinfo:
        await run_preflight(**environment, scratch_dir=scratch_dir)

    assert excinfo.value.assertion == "consolidate_poll_interval"
    assert not scratch_dir.exists()


async def test_live_agent_holds_every_memory_setting(eval_agent: ArcAgent) -> None:
    """Read off the running agent and the built brain, not the emitted TOML."""
    check_memory_settings(eval_agent)


async def test_the_raised_sanitize_cap_reaches_the_built_brain(eval_agent: ArcAgent) -> None:
    """The one setting whose failure is silent, asserted on the object that uses it.

    ``arcmemory.capture`` calls ``sanitize(text, max_length=self._cfg.max_event_chars)``
    and throws the length away. A cap that never arrives leaves that ``_cfg`` at
    arcmemory's own 2000 default, which truncates the tail of a third of this
    corpus's turns and raises nothing anywhere — a memory failure that is really
    a config failure. Read here off the constructed ``ArcMemoryBrain``, not off
    the TOML, because every layer between the two can drop it silently.
    """
    from arcagent.core.agent_lifecycle import activate_runtime_bindings
    from arcagent.modules.memory import _runtime
    from arcmemory.brain import ArcMemoryBrain

    activate_runtime_bindings(eval_agent)
    brain = _runtime.state().brain

    assert isinstance(brain, ArcMemoryBrain)
    assert brain._cfg.max_event_chars == 6000


async def test_drifted_sanitize_cap_is_caught(degraded_agent: AgentFactory) -> None:
    """Back at the backend default, a third of the corpus is silently truncated."""
    agent = await degraded_agent("cap-drift", (_MAX_EVENT_CHARS_LINE, "max_event_chars = 2000"))

    with pytest.raises(PreflightError) as excinfo:
        check_memory_settings(agent)

    assert excinfo.value.assertion == "memory_settings"
    assert "max_event_chars" in str(excinfo.value)


async def test_drifted_recall_budget_is_caught(degraded_agent: AgentFactory) -> None:
    """At a budget below the bundle size, top_k stops meaning what it says."""
    agent = await degraded_agent("budget-drift", (_BUDGET_LINE, "budget = 1024"))

    with pytest.raises(PreflightError) as excinfo:
        check_memory_settings(agent)

    assert excinfo.value.assertion == "memory_settings"
    assert "budget" in str(excinfo.value)


async def test_drifted_event_threshold_is_caught(degraded_agent: AgentFactory) -> None:
    """The outer trigger must never be the thing that declines a driven pass."""
    agent = await degraded_agent(
        "threshold-drift", (_EVENT_THRESHOLD_LINE, "consolidate_event_threshold = 20")
    )

    with pytest.raises(PreflightError) as excinfo:
        check_memory_settings(agent)

    assert excinfo.value.assertion == "memory_settings"
    assert "consolidate_event_threshold" in str(excinfo.value)


async def test_drifted_arcmemory_cadence_is_caught(degraded_agent: AgentFactory) -> None:
    """arcmemory's own gate back at its default makes the other five theatre."""
    agent = await degraded_agent(
        "cadence-drift", (_DYNAMICS_LINE, "consolidate_interval_minutes = 60.0")
    )

    with pytest.raises(PreflightError) as excinfo:
        check_memory_settings(agent)

    assert excinfo.value.assertion == "memory_settings"
    assert "consolidate_interval_minutes" in str(excinfo.value)


# ---------------------------------------------------------------------------
# T-815 — the live seams (REQ-207)
# ---------------------------------------------------------------------------


async def test_live_seams_pass_on_the_real_eval_agent(eval_agent: ArcAgent) -> None:
    """The undegraded agent wires a real brain and all three seams."""
    check_live_seams(eval_agent)


async def test_emptied_distill_provider_is_caught(degraded_agent: AgentFactory) -> None:
    """Without a distiller arcmemory builds no consolidator and mints nothing.

    The agent starts normally and nothing raises — which is exactly how this
    degraded in production.
    """
    agent = await degraded_agent("no-distiller", (_DISTILLER_LINE, 'distill_provider = ""'))

    with pytest.raises(PreflightError) as excinfo:
        check_live_seams(agent)

    assert excinfo.value.assertion == "brain_distiller"


async def test_disabled_embedder_is_caught(degraded_agent: AgentFactory) -> None:
    """`embed_backend = "none"` drops recall to BM25 plus graph, silently."""
    agent = await degraded_agent("no-embedder", (_EMBEDDER_LINE, 'embed_backend = "none"'))

    with pytest.raises(PreflightError) as excinfo:
        check_live_seams(agent)

    assert excinfo.value.assertion == "brain_embedder"


async def test_null_brain_is_caught(degraded_agent: AgentFactory) -> None:
    """A NullBrain writes nothing at all, so the run would score an empty store."""
    agent = await degraded_agent("null-brain", (_BRAIN_LINE, 'brain = "none"'))

    with pytest.raises(PreflightError) as excinfo:
        check_live_seams(agent)

    assert excinfo.value.assertion == "brain_not_null"


async def test_background_consolidation_loop_is_stopped(eval_agent: ArcAgent) -> None:
    """The driven pass runs alone: arcmemory has no lock (REQ-184).

    The loop is spawned by `startup()`, so this is something the preflight has to
    *do* before it fires its own pass — and the read-back is what proves it did.
    """
    registry = eval_agent._capability_registry
    assert await registry.get_task(CONSOLIDATE_LOOP_NAME) is not None

    await _stop_background_consolidation(eval_agent)

    assert await registry.get_task(CONSOLIDATE_LOOP_NAME) is None


def test_missing_daily_note_is_caught(tmp_path: Path) -> None:
    """A pass that reported success and wrote no curated note did not consolidate."""
    (tmp_path / "memory" / "daily-log").mkdir(parents=True)

    with pytest.raises(PreflightError) as excinfo:
        check_daily_log(tmp_path)

    assert excinfo.value.assertion == "daily_log_written"


def test_absent_daily_log_directory_is_caught(tmp_path: Path) -> None:
    """A directory arcmemory never created reads the same as an empty one."""
    with pytest.raises(PreflightError) as excinfo:
        check_daily_log(tmp_path)

    assert excinfo.value.assertion == "daily_log_written"


def test_written_daily_note_passes(tmp_path: Path) -> None:
    daily_log = tmp_path / "memory" / "daily-log"
    daily_log.mkdir(parents=True)
    (daily_log / "2023-09-20.md").write_text("# notes\n", encoding="utf-8")

    check_daily_log(tmp_path)


# ---------------------------------------------------------------------------
# T-816 — the environment gates (REQ-196, REQ-201, REQ-215)
# ---------------------------------------------------------------------------


def _questions() -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads(_ORACLE_FIXTURE.read_bytes())
    return loaded


def _write_dataset(path: Path, questions: list[dict[str, Any]]) -> str:
    """Write a dataset file and return its raw-byte digest."""
    raw = json.dumps(questions).encode("utf-8")
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo carrying both `.gitignore` rules the guard checks.

    `core.excludesFile` is pinned empty so a developer's global gitignore cannot
    make the failure cases pass for the wrong reason.
    """
    assert GIT is not None
    subprocess.run([GIT, "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    excludes = tmp_path / "empty-excludes"
    excludes.write_text("", encoding="utf-8")
    subprocess.run(
        [GIT, "config", "core.excludesFile", str(excludes)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    tmp_path.joinpath(".gitignore").write_text(
        f"{_RUN_DIR_PATTERN}\n{_RESULTS_PATTERN}\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def environment(repo: Path) -> dict[str, Any]:
    """Every `check_environment` argument, all of them passing."""
    dataset_path = repo / "dataset.json"
    sha256 = _write_dataset(dataset_path, _questions())
    return {
        "dataset_path": dataset_path,
        "expected_sha256": sha256,
        "revision": _REVISION,
        "question_ids": [_QUESTION_ID],
        "run_dir": repo / "runs" / "lme-q0001",
        "results_path": repo / "results" / "oracle.jsonl",
        "repo_root": repo,
    }


@requires_git
def test_environment_gates_pass(environment: dict[str, Any]) -> None:
    check_environment(**environment)


@requires_git
async def test_environment_failure_aborts_before_the_scratch_agent(
    environment: dict[str, Any], tmp_path: Path
) -> None:
    """The environment gates cost nothing, so they run before anything is built."""
    environment["expected_sha256"] = "0" * 64
    scratch_dir = tmp_path / "scratch"

    with pytest.raises(PreflightError) as excinfo:
        await run_preflight(**environment, scratch_dir=scratch_dir)

    assert excinfo.value.assertion == "dataset_sha256"
    assert not scratch_dir.exists()


@requires_git
def test_dataset_hash_mismatch_aborts(environment: dict[str, Any]) -> None:
    """A poisoned or drifted download must never reach a scoring run (LLM04)."""
    environment["expected_sha256"] = "0" * 64

    with pytest.raises(PreflightError) as excinfo:
        check_environment(**environment)

    assert excinfo.value.assertion == "dataset_sha256"


@requires_git
def test_missing_dataset_file_aborts(environment: dict[str, Any], tmp_path: Path) -> None:
    environment["dataset_path"] = tmp_path / "absent.json"

    with pytest.raises(PreflightError) as excinfo:
        check_environment(**environment)

    assert excinfo.value.assertion == "dataset_sha256"


@requires_git
def test_unignored_run_dir_aborts(environment: dict[str, Any], repo: Path) -> None:
    """The `.gitignore` contract is enforced by nothing else at run time."""
    repo.joinpath(".gitignore").write_text(f"{_RESULTS_PATTERN}\n", encoding="utf-8")

    with pytest.raises(PreflightError) as excinfo:
        check_environment(**environment)

    assert excinfo.value.assertion == "gitignored_artifacts"
    assert str(environment["run_dir"]) in str(excinfo.value)


@requires_git
def test_unignored_results_file_aborts(environment: dict[str, Any], repo: Path) -> None:
    repo.joinpath(".gitignore").write_text(f"{_RUN_DIR_PATTERN}\n", encoding="utf-8")

    with pytest.raises(PreflightError) as excinfo:
        check_environment(**environment)

    assert excinfo.value.assertion == "gitignored_artifacts"
    assert str(environment["results_path"]) in str(excinfo.value)


@requires_git
def test_question_date_resolves_from_the_derived_fallback(environment: dict[str, Any]) -> None:
    """No per-question field is fine as long as the haystack can anchor it."""
    questions = copy.deepcopy(_questions())
    for question in questions:
        question.pop("question_date", None)
    environment["expected_sha256"] = _write_dataset(environment["dataset_path"], questions)

    check_environment(**environment)


@requires_git
def test_question_with_no_resolvable_date_aborts(environment: dict[str, Any]) -> None:
    """Arc's prompt carries no date, so an unanchored question is not a memory result."""
    questions = copy.deepcopy(_questions())
    for question in questions:
        question.pop("question_date", None)
        question["haystack_dates"] = []
    environment["expected_sha256"] = _write_dataset(environment["dataset_path"], questions)

    with pytest.raises(PreflightError) as excinfo:
        check_environment(**environment)

    assert excinfo.value.assertion == "question_date"
    assert _QUESTION_ID in str(excinfo.value)
