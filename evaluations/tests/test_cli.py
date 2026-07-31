"""COMP-022 CLI tests (T-820 / REQ-203, REQ-208, REQ-213).

Nothing here reaches the network, a provider or a live agent. The CLI's own job
is wiring plus the four refusals it owns — the smoke gate, the dataset pin, the
resume guard and the three named exit paths — so the collaborators those
refusals fire in front of are stubbed at the module attribute the CLI resolves
them through, and every artifact lands under ``tmp_path``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from evaluations.longmemeval import cli
from evaluations.longmemeval.budget import SpendCeilingExceeded
from evaluations.longmemeval.hygiene import RepoHygieneError
from evaluations.longmemeval.preflight import PreflightError
from evaluations.longmemeval.runner import PhaseReport
from evaluations.longmemeval.scoring import QUESTION_TYPES

REPO_ROOT = Path(__file__).resolve().parents[2]
REVISION = "test-revision"


# ---------------------------------------------------------------------------
# Fixtures — a tiny corpus with one question per type
# ---------------------------------------------------------------------------


def _question(question_id: str, question_type: str) -> dict[str, Any]:
    return {
        "question_id": question_id,
        "question_type": question_type,
        "question": "When did the deploy ship?",
        "answer": "Tuesday.",
        "answer_session_ids": [f"{question_id}-s0"],
        "haystack_dates": ["2023/05/20 (Sat) 02:29", "2023/06/01 (Thu) 09:00"],
        "haystack_session_ids": [f"{question_id}-s0", f"{question_id}-s1"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "The deploy shipped Tuesday.", "has_answer": True},
                {"role": "assistant", "content": "Noted."},
            ],
            [
                {"role": "user", "content": "Remind me later."},
                {"role": "assistant", "content": "Will do."},
            ],
        ],
    }


def _write_dataset(tmp_path: Path, *, types: tuple[str, ...] = QUESTION_TYPES) -> Path:
    path = tmp_path / "data" / "corpus.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([_question(f"q{i}", name) for i, name in enumerate(types)]),
        encoding="utf-8",
    )
    return path


def _argv(tmp_path: Path, dataset: Path, *extra: str) -> list[str]:
    return [
        "--dataset",
        str(dataset),
        "--dataset-sha256",
        hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "--dataset-revision",
        REVISION,
        "--runs-root",
        str(tmp_path / "runs"),
        "--results-dir",
        str(tmp_path / "results"),
        *extra,
    ]


class _PassingGuard:
    """Stands in for the repo hygiene guard on paths that live outside the repo."""

    def __init__(self, **_: object) -> None: ...

    def check(self) -> None: ...


class _FailingGuard:
    def __init__(self, **_: object) -> None: ...

    def check(self) -> None:
        raise RepoHygieneError("evaluations/runs is not ignored by git")


def _report(**overrides: Any) -> PhaseReport:
    fields: dict[str, Any] = {
        "phase": "full-s",
        "requested": 3,
        "skipped": 0,
        "complete": 3,
        "void": 0,
        "error": 0,
        "stratification": None,
        "wall_seconds": 1.5,
    }
    return PhaseReport(**{**fields, **overrides})


def _stub_runner(report: PhaseReport, seen: dict[str, Any] | None = None) -> type:
    """A PhaseRunner stand-in that records its wiring and returns ``report``."""

    class _Runner:
        def __init__(self, **kwargs: Any) -> None:
            if seen is not None:
                seen.update(kwargs)

        async def run(self, phase: str, *, strategy: str | None = None) -> PhaseReport:
            if seen is not None:
                seen["phase"] = phase
                seen["strategy"] = strategy
            return report

    return _Runner


@pytest.fixture
def passing_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "RepoHygieneGuard", _PassingGuard)


# ---------------------------------------------------------------------------
# The invocation contract: module only, no package, no console script
# ---------------------------------------------------------------------------


def test_module_docstring_documents_the_module_invocation() -> None:
    assert cli.__doc__ is not None
    assert "python -m evaluations.longmemeval.cli" in cli.__doc__
    assert "ModuleNotFoundError" in cli.__doc__


def test_help_documents_the_module_invocation_and_why_the_script_path_fails() -> None:
    help_text = cli.build_parser().format_help()

    assert "uv run python -m evaluations.longmemeval.cli" in help_text
    assert "python evaluations/longmemeval/cli.py" in help_text
    assert "ModuleNotFoundError: No module named 'evaluations'" in help_text


def test_evaluations_ships_no_package_and_no_console_script() -> None:
    """The CLI is reachable only as a module, exactly as COMP-022 specifies."""
    raw = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    config = tomllib.loads(raw)

    assert config["tool"]["uv"]["workspace"]["members"] == ["packages/*"]
    assert "longmemeval" not in raw
    assert not (REPO_ROOT / "evaluations" / "pyproject.toml").exists()


# ---------------------------------------------------------------------------
# The flag surface
# ---------------------------------------------------------------------------


def test_parser_exposes_every_required_flag() -> None:
    args = cli.build_parser().parse_args(
        ["--phase", "oracle", "--keep-workspace-on-failure", "--resume"]
    )

    assert args.phase == "oracle"
    assert args.keep_workspace_on_failure is True
    assert args.resume is True
    assert args.dry_run is False
    assert args.smoke is None


@pytest.mark.parametrize("phase", ["oracle", "sample-s", "full-s"])
def test_parser_accepts_all_three_phases(phase: str) -> None:
    assert cli.build_parser().parse_args(["--phase", phase]).phase == phase


@pytest.mark.parametrize("count", ["2", "6", "0", "notanumber"])
def test_smoke_count_outside_three_to_five_is_a_usage_error(count: str) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--smoke", count])

    assert exc.value.code == 2


def test_naming_no_run_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([])

    assert exc.value.code == 2


def test_sample_s_without_a_strategy_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--phase", "sample-s"])

    assert exc.value.code == 2


def test_smoke_and_phase_together_are_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--smoke", "3", "--phase", "full-s"])

    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# The smoke gate (REQ-203)
# ---------------------------------------------------------------------------


def test_full_s_is_refused_when_the_smoke_gate_has_not_passed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path)

    code = cli.main(_argv(tmp_path, dataset, "--phase", "full-s"))

    assert code == cli.EXIT_SMOKE_GATE
    err = capsys.readouterr().err
    assert "smoke_gate_passed" in err
    assert "--smoke" in err
    # Refused before anything was created: the gate is the cheapest possible stop.
    assert not (tmp_path / "results").exists()
    assert not (tmp_path / "runs").exists()


def test_full_s_gets_past_the_gate_once_a_smoke_has_passed(tmp_path: Path) -> None:
    dataset = _write_dataset(tmp_path)
    cli.write_smoke_gate(
        tmp_path / "results" / cli.SMOKE_GATE_FILENAME, question_ids=["q0", "q1", "q2"]
    )

    # The pin is deliberately omitted, so the next refusal after the gate is the
    # one that proves the gate itself let the run through.
    code = cli.main(
        [
            "--dataset",
            str(dataset),
            "--runs-root",
            str(tmp_path / "runs"),
            "--results-dir",
            str(tmp_path / "results"),
            "--phase",
            "full-s",
        ]
    )

    assert code == cli.EXIT_ERROR


def test_oracle_and_sample_s_are_not_gated_by_the_smoke(tmp_path: Path) -> None:
    """Only full-s costs ~40,000 ingest calls, so only full-s is gated."""
    dataset = _write_dataset(tmp_path)

    code = cli.main(
        [
            "--dataset",
            str(dataset),
            "--runs-root",
            str(tmp_path / "runs"),
            "--results-dir",
            str(tmp_path / "results"),
            "--phase",
            "oracle",
        ]
    )

    assert code == cli.EXIT_ERROR  # the missing dataset pin, not the smoke gate


def test_smoke_selection_spans_distinct_question_types(tmp_path: Path) -> None:
    from evaluations.longmemeval.dataset import load_dataset

    dataset = load_dataset(_write_dataset(tmp_path), revision=REVISION)

    chosen = cli.select_smoke_questions(dataset, count=4)

    assert len(chosen) == 4
    assert len({raw["question_type"] for raw in chosen}) == 4


def test_smoke_selection_refuses_a_corpus_that_cannot_span_the_count(tmp_path: Path) -> None:
    from evaluations.longmemeval.dataset import load_dataset

    dataset = load_dataset(
        _write_dataset(tmp_path, types=("multi-session", "knowledge-update")), revision=REVISION
    )

    with pytest.raises(cli.SmokeGateError, match="cannot cover one type each"):
        cli.select_smoke_questions(dataset, count=3)


def test_a_clean_smoke_writes_the_gate_that_opens_full_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passing_guard: None
) -> None:
    dataset = _write_dataset(tmp_path)
    monkeypatch.setattr(cli, "PhaseRunner", _stub_runner(_report(complete=3)))

    code = cli.main(_argv(tmp_path, dataset, "--smoke", "3"))

    assert code == cli.EXIT_OK
    gate = json.loads((tmp_path / "results" / cli.SMOKE_GATE_FILENAME).read_text())
    assert gate["question_ids"] == ["q0", "q1", "q2"]
    assert gate["unlocks"] == "full-s"
    cli.assert_smoke_gate_passed(tmp_path / "results" / cli.SMOKE_GATE_FILENAME)


def test_a_smoke_with_an_errored_question_leaves_the_gate_shut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passing_guard: None
) -> None:
    dataset = _write_dataset(tmp_path)
    monkeypatch.setattr(cli, "PhaseRunner", _stub_runner(_report(complete=2, error=1)))

    code = cli.main(_argv(tmp_path, dataset, "--smoke", "3"))

    assert code == cli.EXIT_SMOKE_GATE
    assert not (tmp_path / "results" / cli.SMOKE_GATE_FILENAME).exists()


def test_the_smoke_runs_the_selected_subset_through_the_real_phase_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passing_guard: None
) -> None:
    dataset = _write_dataset(tmp_path)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "PhaseRunner", _stub_runner(_report(requested=5, complete=5), seen))

    assert cli.main(_argv(tmp_path, dataset, "--smoke", "5")) == cli.EXIT_OK

    assert seen["phase"] == "full-s"
    assert seen["strategy"] is None
    assert len(seen["dataset"].questions) == 5


# ---------------------------------------------------------------------------
# The three named exit paths
# ---------------------------------------------------------------------------


def test_a_preflight_failure_exits_non_zero_naming_the_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    passing_guard: None,
) -> None:
    dataset = _write_dataset(tmp_path)

    async def _fail(**_: object) -> None:
        raise PreflightError("brain_distiller", "ArcMemoryBrain._distiller is None")

    monkeypatch.setattr(cli, "run_preflight", _fail)

    code = cli.main(_argv(tmp_path, dataset, "--phase", "oracle"))

    assert code == cli.EXIT_PREFLIGHT
    err = capsys.readouterr().err
    assert "[brain_distiller]" in err
    assert "_distiller is None" in err


def test_a_repo_hygiene_failure_exits_non_zero_naming_the_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path)
    monkeypatch.setattr(cli, "RepoHygieneGuard", _FailingGuard)

    code = cli.main(_argv(tmp_path, dataset, "--phase", "oracle"))

    assert code == cli.EXIT_HYGIENE
    err = capsys.readouterr().err
    assert "[gitignored_artifacts]" in err
    assert "not ignored by git" in err
    # The guard writes nothing and runs before the ledger creates its file.
    assert not (tmp_path / "results" / "oracle.jsonl").exists()


def test_a_spend_ceiling_breach_exits_non_zero_naming_the_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    passing_guard: None,
) -> None:
    dataset = _write_dataset(tmp_path)

    class _Ceiling:
        def __init__(self, **_: object) -> None: ...

        async def run(self, phase: str, *, strategy: str | None = None) -> PhaseReport:
            raise SpendCeilingExceeded(spent_usd=44.0, ceiling_usd=40.0, estimate_usd=36.36)

    monkeypatch.setattr(cli, "PhaseRunner", _Ceiling)

    code = cli.main(_argv(tmp_path, dataset, "--phase", "oracle"))

    assert code == cli.EXIT_CEILING
    err = capsys.readouterr().err
    assert "[spend_ceiling]" in err
    assert "$44.00" in err
    assert "$40.00" in err


def test_the_three_named_exit_codes_are_distinct_and_non_zero() -> None:
    codes = {cli.EXIT_PREFLIGHT, cli.EXIT_HYGIENE, cli.EXIT_CEILING}

    assert len(codes) == 3
    assert 0 not in codes
    # 2 is argparse's usage error; reusing it would make a gate look like a typo.
    assert 2 not in codes


# ---------------------------------------------------------------------------
# --dry-run (REQ-208)
# ---------------------------------------------------------------------------


def test_dry_run_prints_the_estimate_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path)

    code = cli.main(_argv(tmp_path, dataset, "--phase", "full-s", "--dry-run"))

    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "estimated cost" in out
    assert "abort ceiling" in out
    assert "llm calls" in out
    assert "dry run" in out


def test_dry_run_creates_no_artifacts(tmp_path: Path) -> None:
    dataset = _write_dataset(tmp_path)

    assert cli.main(_argv(tmp_path, dataset, "--phase", "oracle", "--dry-run")) == cli.EXIT_OK

    assert not (tmp_path / "results").exists()
    assert not (tmp_path / "runs").exists()


def test_dry_run_says_so_plainly_when_the_corpus_was_never_downloaded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        [
            "--phase",
            "oracle",
            "--dry-run",
            "--dataset",
            str(tmp_path / "absent.json"),
            "--results-dir",
            str(tmp_path / "results"),
        ]
    )

    assert code == cli.EXIT_ERROR
    assert "is not present" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --resume and --keep-workspace-on-failure (REQ-213)
# ---------------------------------------------------------------------------


def test_appending_to_an_existing_results_file_needs_resume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path)
    results = tmp_path / "results" / "oracle.jsonl"
    results.parent.mkdir(parents=True)
    results.write_text('{"question_id": "q0"}\n', encoding="utf-8")

    code = cli.main(_argv(tmp_path, dataset, "--phase", "oracle"))

    assert code == cli.EXIT_ERROR
    assert "--resume" in capsys.readouterr().err


def test_resume_reopens_an_existing_results_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passing_guard: None
) -> None:
    dataset = _write_dataset(tmp_path)
    results = tmp_path / "results" / "oracle.jsonl"
    results.parent.mkdir(parents=True)
    results.write_text('{"question_id": "q0"}\n', encoding="utf-8")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "PhaseRunner", _stub_runner(_report(phase="oracle"), seen))

    assert cli.main(_argv(tmp_path, dataset, "--phase", "oracle", "--resume")) == cli.EXIT_OK

    assert seen["phase"] == "oracle"


def test_keep_workspace_on_failure_reaches_the_phase_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passing_guard: None
) -> None:
    dataset = _write_dataset(tmp_path)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "PhaseRunner", _stub_runner(_report(phase="oracle"), seen))

    code = cli.main(
        _argv(tmp_path, dataset, "--phase", "oracle", "--keep-workspace-on-failure")
    )

    assert code == cli.EXIT_OK
    assert seen["keep_workspace_on_failure"] is True


def test_the_ceiling_is_wired_to_its_own_cost_ledger_beside_the_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passing_guard: None
) -> None:
    dataset = _write_dataset(tmp_path)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "PhaseRunner", _stub_runner(_report(phase="oracle"), seen))

    assert cli.main(_argv(tmp_path, dataset, "--phase", "oracle")) == cli.EXIT_OK

    assert seen["budget"].ceiling_usd > 0
    assert seen["runs_root"] == tmp_path / "runs"


# ---------------------------------------------------------------------------
# The dataset pin (REQ-201)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "omit", ["--dataset-sha256", "--dataset-revision"]
)
def test_a_scored_run_refuses_to_start_without_the_dataset_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], omit: str
) -> None:
    dataset = _write_dataset(tmp_path)
    argv = _argv(tmp_path, dataset, "--phase", "oracle")
    index = argv.index(omit)
    del argv[index : index + 2]

    code = cli.main(argv)

    assert code == cli.EXIT_ERROR
    assert omit in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Resolved model ids
# ---------------------------------------------------------------------------


def test_model_ids_are_read_from_the_config_surfaces_not_re_declared() -> None:
    """A blank id is the silent-degrade signature the provenance block rejects."""
    models = cli.resolved_model_ids()

    assert set(models) == {"agent", "judge", "embedder", "distiller"}
    assert all(value for value in models.values())
    assert "/" in models["agent"]  # arcllm's provider/model form


def test_resolving_model_ids_writes_nothing(tmp_path: Path) -> None:
    before = sorted(p.name for p in cli.RUNS_ROOT.iterdir()) if cli.RUNS_ROOT.exists() else None

    cli.resolved_model_ids()

    after = sorted(p.name for p in cli.RUNS_ROOT.iterdir()) if cli.RUNS_ROOT.exists() else None
    assert after == before


def test_usage_check_rejects_a_strategy_on_a_phase_that_ignores_it() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["--phase", "oracle", "--strategy", "even"])

    with pytest.raises(SystemExit) as exc:
        cli._check_usage(parser, args)

    assert exc.value.code == 2


def test_smoke_count_type_rejects_a_non_integer() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="not an integer"):
        cli._smoke_count("three")
