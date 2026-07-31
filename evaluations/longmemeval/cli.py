# ruff: noqa: T201 — CLI tool; print is the right primitive here.
"""The harness entry point (COMP-022 / REQ-203, REQ-208, REQ-213).

Wires the merged components together and nothing else: the hygiene guard, the
dry-run estimator, the budget governor, the preflight, the result ledger and the
phase runner all already exist and are driven here through their public
contracts. No measurement logic lives in this file.

*Three phases, each entered by naming it* (REQ-203). ``oracle`` proves the path
across 500 near-free questions, ``sample-s`` measures a stratified ~50, and
``full-s`` spends the ~40,000 ingest calls. Nothing advances automatically, and
``full-s`` additionally requires that ``--smoke N`` has already run 3-5 questions
spanning question types end to end and left its gate behind.

*One question, watched* (``--question-id ID``, repeatable). The smallest run there
is: the named question(s) go through ingest, consolidate, query and judge, and
what the agent answered is printed beside the gold answer and the judge's call.
It defaults to the cheap oracle corpus, writes into a fresh directory of its own
so a one-off cannot pollute a scored phase's ledger, and opens nothing — only
``--smoke`` sets the ``full-s`` gate.

*Invocation.* ``evaluations/`` is deliberately not a uv workspace member, so
there is no package, no install and no console-script entry. Run it as a module
from the repository root::

    uv run python -m evaluations.longmemeval.cli --phase oracle \\
        --dataset-sha256 <sha256> --dataset-revision <hf-revision>

``python evaluations/longmemeval/cli.py`` does **not** work. Python puts the
script's own directory on ``sys.path`` rather than the repository root, so the
very first import fails with ``ModuleNotFoundError: No module named
'evaluations'``.

*Exit codes.* ``0`` on a clean phase. A preflight, repo-hygiene or spend-ceiling
failure exits non-zero with the failed assertion named, because "the run
aborted" is not actionable and "assertion 'brain_distiller' failed" is.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tomllib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcllm.embeddings import DEFAULT_EMBED_MODEL

from evaluations.longmemeval.adapter import LongMemEvalAdapter, QuestionNotFoundError
from evaluations.longmemeval.budget import (
    CEILING_FRACTION,
    CURRENT_PRICING_TABLE_VERSION,
    MAX_EVENT_CHARS,
    BudgetGovernor,
    CallProfile,
    DatasetUnavailableError,
    Estimate,
    SpendCeilingExceeded,
    estimate_run,
)
from evaluations.longmemeval.dataset import Dataset, load_dataset
from evaluations.longmemeval.hygiene import RepoHygieneError, RepoHygieneGuard
from evaluations.longmemeval.ingest.agent_factory import (
    ARCLLM_EVAL_CONFIG,
    ARCRUN_EVAL_CONFIG,
    render_eval_agent_config,
)
from evaluations.longmemeval.judge import JUDGE_MODEL_NAME
from evaluations.longmemeval.ledger import ResultLedger, ResultRow
from evaluations.longmemeval.manifest import MeasurementScope, RunManifest, build_provenance
from evaluations.longmemeval.paths import (
    DATA_DIR,
    RESULTS_DIR,
    RUNS_ROOT,
)
from evaluations.longmemeval.preflight import PREFLIGHT_QUESTION_ID, PreflightError, run_preflight
from evaluations.longmemeval.runner import (
    PHASES,
    Phase,
    PhaseReport,
    PhaseRunner,
    PreflightHook,
    StratificationStrategy,
    resolve_phase,
)
from evaluations.longmemeval.scoring import QUESTION_TYPES

PHASE_DATASETS: dict[str, str] = {
    "oracle": "longmemeval_oracle.json",
    "sample-s": "longmemeval_s_cleaned.json",
    "full-s": "longmemeval_s_cleaned.json",
}
"""Which corpus each phase measures. Oracle is its own file; both S phases share one."""

SMOKE_GATE_FILENAME = "smoke_gate.json"
SMOKE_DIRNAME = "smoke"
SMOKE_MIN = 3
SMOKE_MAX = 5
"""The smoke runs 3-5 questions: fewer cannot span types, more is no longer a smoke."""

QUESTION_DIRNAME = "questions"
QUESTION_RESULTS_FILENAME = "results.jsonl"
QUESTION_DEFAULT_PHASE: Phase = "oracle"
"""Where a ``--question-id`` run lands, and which corpus it reads by default.

Each run gets its own timestamped directory under ``<results-dir>/questions``, so
a one-off can neither append to a scored phase's ledger nor overwrite the last
one-off's manifest."""

EXIT_OK = 0
EXIT_ERROR = 1
# 2 belongs to argparse's own usage errors, so the named gates start at 3.
EXIT_PREFLIGHT = 3
EXIT_HYGIENE = 4
EXIT_CEILING = 5
EXIT_SMOKE_GATE = 6

_CONFIG_PROBE_ID = "config-probe"
"""Names the throwaway render used to read model ids. Nothing is written for it."""


class SmokeGateError(RuntimeError):
    """``--phase full-s`` was entered without a passing smoke, or the smoke failed."""


class DatasetPinRequiredError(RuntimeError):
    """A scored run was started without the dataset SHA-256 and HF revision (REQ-201)."""


class ResumeRefusedError(RuntimeError):
    """Results already exist and ``--resume`` was not passed."""


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The full flag surface, with the module invocation spelled out in ``--help``."""
    parser = argparse.ArgumentParser(
        prog="python -m evaluations.longmemeval.cli",
        description="Run the LongMemEval harness against the real Arc stack.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--phase",
        choices=PHASES,
        help="which gated phase to run; never defaulted, since the three differ by "
        "roughly two orders of magnitude in spend (REQ-203)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="walk the phase's corpus through the real chunker and print the projected "
        "spend without making a single call (REQ-208)",
    )
    parser.add_argument(
        "--smoke",
        type=_smoke_count,
        metavar="N",
        help=f"run N ({SMOKE_MIN}-{SMOKE_MAX}) questions spanning question types end to "
        "end on the S corpus; passing it is what opens --phase full-s",
    )
    parser.add_argument(
        "--question-id",
        action="append",
        metavar="ID",
        help="run exactly this question end to end and print what it answered; repeat "
        f"the flag for more. Reads {PHASE_DATASETS[QUESTION_DEFAULT_PHASE]} unless "
        "--dataset says otherwise, writes each run to a new directory under "
        f"<results-dir>/{QUESTION_DIRNAME}, and never opens the --phase full-s gate",
    )
    parser.add_argument(
        "--keep-workspace-on-failure",
        action="store_true",
        help="retain the workspace of any question that voids or errors, instead of "
        "tearing it down (REQ-213)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="continue an existing results file, skipping the questions already "
        "recorded complete; required to append to one at all",
    )
    parser.add_argument(
        "--strategy",
        choices=("even", "proportional"),
        help="how sample-s draws its ~50 questions; required by that phase and "
        "rejected by the others",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        help=f"dataset file; defaults to the phase's own under {DATA_DIR}",
    )
    parser.add_argument(
        "--dataset-sha256",
        help="the pinned SHA-256 of the dataset's raw bytes; required for any scored "
        "run (REQ-201)",
    )
    parser.add_argument(
        "--dataset-revision",
        help="the HuggingFace revision the dataset was downloaded at; required for any "
        "scored run, because the 'cleaned' revision is not comparable to the original",
    )
    parser.add_argument("--runs-root", type=Path, default=RUNS_ROOT)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--pricing-table-version", default=CURRENT_PRICING_TABLE_VERSION)
    return parser


_EPILOG = f"""\
Run from the repository root:

  uv run python -m evaluations.longmemeval.cli --phase oracle \\
      --dataset-sha256 <sha256> --dataset-revision <hf-revision>
  uv run python -m evaluations.longmemeval.cli --phase full-s --dry-run
  uv run python -m evaluations.longmemeval.cli --smoke {SMOKE_MIN} \\
      --dataset-sha256 <sha256> --dataset-revision <hf-revision>
  uv run python -m evaluations.longmemeval.cli --question-id <question-id> \\
      --dataset-sha256 <sha256> --dataset-revision <hf-revision>

`python evaluations/longmemeval/cli.py` does NOT work: Python puts the script's
own directory on sys.path instead of the repository root, so the first import
fails with `ModuleNotFoundError: No module named 'evaluations'`. There is no
console-script entry either -- evaluations/ is deliberately outside the uv
workspace.

Exit codes:
  0  clean phase                    {EXIT_HYGIENE}  repo hygiene assertion failed
  2  usage error                    {EXIT_CEILING}  spend ceiling reached
  {EXIT_PREFLIGHT}  preflight assertion failed     {EXIT_SMOKE_GATE}  smoke gate not passed
  {EXIT_ERROR}  everything else
"""


def _smoke_count(value: str) -> int:
    """Parse ``--smoke N``, refusing a count that cannot span question types."""
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if not SMOKE_MIN <= count <= SMOKE_MAX:
        raise argparse.ArgumentTypeError(
            f"--smoke takes {SMOKE_MIN}-{SMOKE_MAX} questions, got {count}"
        )
    return count


def _check_usage(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject flag combinations that name no run, or two of them."""
    if args.question_id:
        if args.phase is not None:
            parser.error("--question-id names its own run; do not also pass --phase")
        if args.smoke is not None:
            parser.error("--question-id and --smoke are separate runs; pass one")
        if args.dry_run:
            parser.error("--question-id and --dry-run are separate runs; pass one")
        if args.strategy is not None:
            parser.error("--strategy is not applied by --question-id")
        return
    if args.smoke is not None:
        if args.dry_run:
            parser.error("--smoke and --dry-run are separate runs; pass one")
        if args.phase is not None:
            parser.error("--smoke always runs the S corpus; do not also pass --phase")
        return
    if args.phase is None:
        parser.error("name a run: --phase {oracle,sample-s,full-s} or --smoke N")
    if args.phase == "sample-s" and args.strategy is None:
        parser.error("--phase sample-s needs --strategy {even,proportional}")
    if args.phase != "sample-s" and args.strategy is not None:
        parser.error(f"--strategy is not applied by --phase {args.phase}")


# ---------------------------------------------------------------------------
# The smoke gate
# ---------------------------------------------------------------------------


def select_smoke_questions(dataset: Dataset, *, count: int) -> list[dict[str, Any]]:
    """Take the first question of each of ``count`` distinct types (REQ-203).

    Spanning types is the whole point: a smoke of three ``multi-session``
    questions proves one retrieval shape works and says nothing about the other
    five, which is exactly the false confidence the gate exists to prevent.
    """
    first_of_type: dict[str, dict[str, Any]] = {}
    for raw in dataset.questions:
        first_of_type.setdefault(str(raw["question_type"]), raw)
    chosen = [first_of_type[name] for name in QUESTION_TYPES if name in first_of_type]
    if len(chosen) < count:
        raise SmokeGateError(
            f"the dataset spans {len(chosen)} question type(s), so a {count}-question "
            "smoke cannot cover one type each"
        )
    return chosen[:count]


def assert_smoke_gate_passed(gate_path: Path) -> None:
    """Refuse ``--phase full-s`` until a smoke has proven the path (REQ-203)."""
    if not gate_path.is_file():
        raise SmokeGateError(
            f"no smoke gate at {gate_path}: full-s spends roughly 40,000 ingest calls, so "
            f"it is entered only after --smoke N runs {SMOKE_MIN}-{SMOKE_MAX} questions "
            "spanning question types end to end"
        )


def write_smoke_gate(gate_path: Path, *, question_ids: Sequence[str]) -> Path:
    """Record the passing smoke, naming the questions it actually proved."""
    gate_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "passed_at_utc": datetime.now(UTC).isoformat(),
        "question_ids": list(question_ids),
        "unlocks": "full-s",
    }
    gate_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return gate_path


# ---------------------------------------------------------------------------
# Named questions
# ---------------------------------------------------------------------------


def select_questions(dataset: Dataset, *, question_ids: Sequence[str]) -> list[dict[str, Any]]:
    """The named questions, in the order asked, refusing an id the corpus lacks.

    A typo'd id must be named and refused here, before the run spends anything:
    silently running nothing looks exactly like a question that produced no
    output. Repeats collapse, because paying to ingest one haystack twice in a
    single run measures nothing new.
    """
    by_id = {str(raw["question_id"]): raw for raw in dataset.questions}
    wanted = list(dict.fromkeys(question_ids))
    missing = [question_id for question_id in wanted if question_id not in by_id]
    if missing:
        raise QuestionNotFoundError(
            f"no question {', '.join(repr(name) for name in missing)} in the loaded "
            f"corpus of {len(by_id)} questions; check the id, or point --dataset at the "
            "corpus that holds it"
        )
    return [by_id[question_id] for question_id in wanted]


# ---------------------------------------------------------------------------
# Resolved run configuration
# ---------------------------------------------------------------------------


def resolved_model_ids() -> dict[str, str]:
    """The four model ids this run uses, read from the config surfaces themselves.

    Read rather than re-declared: a second copy of a model id in this file is
    exactly how a manifest ends up naming a model the agent never used. The
    probe render is pure — it touches no filesystem — and its ``run_dir`` reaches
    only the arcstore path, which is not read here.
    """
    agent_config = render_eval_agent_config(
        question_id=_CONFIG_PROBE_ID, run_dir=RUNS_ROOT / _CONFIG_PROBE_ID
    )
    memory = tomllib.loads(agent_config)["modules"]["memory"]["config"]
    return {
        "agent": str(tomllib.loads(ARCLLM_EVAL_CONFIG)["llm"]["model"]),
        "judge": JUDGE_MODEL_NAME,
        # An empty embed_model means "arcllm's offline default" — a real model id
        # that the provenance block has to name rather than leave blank.
        "embedder": str(memory["embed_model"]) or DEFAULT_EMBED_MODEL,
        "distiller": str(memory["distill_model"]),
    }


def _resolved_config(
    *,
    phase: Phase,
    dataset: Dataset,
    dataset_path: Path,
    strategy: StratificationStrategy | None,
    pricing_version: str,
    models: dict[str, str],
) -> dict[str, Any]:
    """What this run resolved to, as the config hash covers it.

    The agent's own ``arcagent.toml`` is deliberately left out: it embeds the
    per-question run directory, so hashing it would make two otherwise identical
    runs disagree about their config. What an environment variable can actually
    repoint — the four model ids, and the two run-dir-independent config texts —
    is all here.
    """
    return {
        "phase": phase,
        "dataset": dataset_path.name,
        "dataset_sha256": dataset.sha256,
        "dataset_revision": dataset.revision,
        "stratification_strategy": strategy,
        "pricing_table_version": pricing_version,
        "max_event_chars": MAX_EVENT_CHARS,
        "models": models,
        "arcllm_toml": ARCLLM_EVAL_CONFIG,
        "arcrun_toml": ARCRUN_EVAL_CONFIG,
    }


def _build_manifest(
    *,
    phase: Phase,
    dataset: Dataset,
    dataset_path: Path,
    estimate: Estimate,
    strategy: StratificationStrategy | None,
    pricing_version: str,
) -> RunManifest:
    """Assemble the one manifest every result row carries a copy of (REQ-210)."""
    models = resolved_model_ids()
    return RunManifest(
        provenance=build_provenance(
            resolved_config=_resolved_config(
                phase=phase,
                dataset=dataset,
                dataset_path=dataset_path,
                strategy=strategy,
                pricing_version=pricing_version,
                models=models,
            ),
            dataset=dataset,
            agent_model_id=models["agent"],
            judge_model_id=models["judge"],
            embedder_model_id=models["embedder"],
            distiller_model_id=models["distiller"],
        ),
        measurement_scope=MeasurementScope(workpad_enabled=True, policy_enabled=True),
        session_ingest_order={
            str(raw["question_id"]): LongMemEvalAdapter(
                dataset=dataset, question_id=str(raw["question_id"])
            ).session_ingest_order
            for raw in dataset.questions
        },
        dry_run_estimate=estimate.model_dump(mode="json"),
        pricing_table_version=pricing_version,
    )


# ---------------------------------------------------------------------------
# The runs
# ---------------------------------------------------------------------------


def _default_dataset(phase: Phase) -> Path:
    return DATA_DIR / PHASE_DATASETS[phase]


def _load(path: Path, *, expected_sha256: str | None, revision: str) -> Dataset:
    """Load the corpus under its pin, saying so plainly when it was never downloaded."""
    if not path.is_file():
        raise DatasetUnavailableError(path)
    return load_dataset(path, expected_sha256=expected_sha256, revision=revision)


def _require_dataset_pin(args: argparse.Namespace) -> tuple[str, str]:
    """The SHA-256 and HF revision a scored run must be pinned to (REQ-201)."""
    missing = [
        flag
        for flag, value in (
            ("--dataset-sha256", args.dataset_sha256),
            ("--dataset-revision", args.dataset_revision),
        )
        if not value
    ]
    if missing:
        raise DatasetPinRequiredError(
            f"a scored run needs {' and '.join(missing)}: the dataset is a third-party "
            "download feeding a memory store, and the Sept-2025 'cleaned' revision is not "
            "numerically comparable to the original (REQ-201)"
        )
    return str(args.dataset_sha256), str(args.dataset_revision)


def _assert_resumable(results_path: Path, *, resume: bool) -> None:
    """Refuse to append to an existing results file unless resuming it on purpose.

    Every question already recorded complete is silently skipped, so appending
    by accident produces a phase report describing a run that mostly did not
    happen.
    """
    if resume or not results_path.exists() or results_path.stat().st_size == 0:
        return
    raise ResumeRefusedError(
        f"{results_path} already holds results, and every question recorded complete in "
        "it would be skipped. Pass --resume to continue that run, or move the file aside "
        "to start a new one"
    )


def _preflight_hook(
    *,
    dataset_path: Path,
    expected_sha256: str,
    revision: str,
    runs_root: Path,
    results_path: Path,
) -> PreflightHook:
    """Bind COMP-020's gates to this run; the phase runner supplies the question ids."""

    async def hook(*, question_ids: Sequence[str]) -> None:
        await run_preflight(
            dataset_path=dataset_path,
            expected_sha256=expected_sha256,
            revision=revision,
            question_ids=question_ids,
            run_dir=runs_root,
            results_path=results_path,
            scratch_dir=runs_root / PREFLIGHT_QUESTION_ID,
        )

    return hook


async def _execute(
    *,
    phase: Phase,
    dataset: Dataset,
    dataset_path: Path,
    expected_sha256: str,
    revision: str,
    strategy: StratificationStrategy | None,
    runs_root: Path,
    results_dir: Path,
    results_path: Path,
    keep_workspace_on_failure: bool,
    pricing_version: str,
) -> PhaseReport:
    """Wire the merged components together and run one phase.

    The hygiene guard goes first because it writes nothing: a repo that would
    leak an artifact fails before the ledger creates its file (REQ-196).
    """
    RepoHygieneGuard(run_dir=runs_root, results_path=results_path).check()

    estimate = estimate_run(dataset, profile=CallProfile(), pricing_table_version=pricing_version)
    manifest = _build_manifest(
        phase=phase,
        dataset=dataset,
        dataset_path=dataset_path,
        estimate=estimate,
        strategy=strategy,
        pricing_version=pricing_version,
    )
    with ResultLedger(results_path) as ledger:
        runner = PhaseRunner(
            dataset=dataset,
            manifest=manifest,
            ledger=ledger,
            budget=BudgetGovernor(
                estimate=estimate, ledger_path=results_path.with_suffix(".costs.jsonl")
            ),
            runs_root=runs_root,
            results_dir=results_dir,
            preflight=_preflight_hook(
                dataset_path=dataset_path,
                expected_sha256=expected_sha256,
                revision=revision,
                runs_root=runs_root,
                results_path=results_path,
            ),
            keep_workspace_on_failure=keep_workspace_on_failure,
        )
        return await runner.run(phase, strategy=strategy)


def _dry_run(args: argparse.Namespace) -> int:
    """Project the phase's spend through the real chunker, calling nothing (REQ-208)."""
    phase = resolve_phase(args.phase)
    dataset_path: Path = args.dataset or _default_dataset(phase)
    dataset = _load(
        dataset_path,
        expected_sha256=args.dataset_sha256,
        # The revision is provenance, and a dry run produces none: its estimate
        # carries no revision field, so an unpinned one cannot escape into a result.
        revision=args.dataset_revision or "unpinned",
    )
    estimate = estimate_run(
        dataset, profile=CallProfile(), pricing_table_version=args.pricing_table_version
    )
    _print_estimate(estimate, phase=phase, dataset_path=dataset_path)
    return EXIT_OK


async def _phase(args: argparse.Namespace) -> int:
    """Run one gated phase end to end and report what it did."""
    phase = resolve_phase(args.phase)
    results_dir: Path = args.results_dir
    if phase == "full-s":
        assert_smoke_gate_passed(results_dir / SMOKE_GATE_FILENAME)

    expected_sha256, revision = _require_dataset_pin(args)
    dataset_path: Path = args.dataset or _default_dataset(phase)
    results_path = results_dir / f"{phase}.jsonl"
    _assert_resumable(results_path, resume=args.resume)

    report = await _execute(
        phase=phase,
        dataset=_load(dataset_path, expected_sha256=expected_sha256, revision=revision),
        dataset_path=dataset_path,
        expected_sha256=expected_sha256,
        revision=revision,
        strategy=args.strategy,
        runs_root=args.runs_root,
        results_dir=results_dir,
        results_path=results_path,
        keep_workspace_on_failure=args.keep_workspace_on_failure,
        pricing_version=args.pricing_table_version,
    )
    _print_report(report, results_path=results_path)
    return EXIT_OK


async def _smoke(args: argparse.Namespace) -> int:
    """Prove the whole path on 3-5 questions spanning types, then open full-s."""
    expected_sha256, revision = _require_dataset_pin(args)
    dataset_path: Path = args.dataset or _default_dataset("full-s")
    dataset = _load(dataset_path, expected_sha256=expected_sha256, revision=revision)
    questions = select_smoke_questions(dataset, count=args.smoke)
    question_ids = [str(raw["question_id"]) for raw in questions]

    smoke_dir: Path = args.results_dir / SMOKE_DIRNAME
    results_path = smoke_dir / "smoke.jsonl"
    _assert_resumable(results_path, resume=args.resume)

    # Labelled full-s because that is what it is: the same corpus and the same
    # code path, three to five questions of it. PhaseRunner runs every question
    # in the dataset it is handed for any phase but sample-s, so handing it the
    # subset IS the selection — no second selection path exists to drift.
    report = await _execute(
        phase="full-s",
        dataset=dataset.model_copy(update={"questions": questions}),
        dataset_path=dataset_path,
        expected_sha256=expected_sha256,
        revision=revision,
        strategy=None,
        runs_root=args.runs_root,
        results_dir=smoke_dir,
        results_path=results_path,
        keep_workspace_on_failure=args.keep_workspace_on_failure,
        pricing_version=args.pricing_table_version,
    )
    _print_report(report, results_path=results_path)

    if report.complete != len(question_ids):
        return _abort(
            EXIT_SMOKE_GATE,
            "smoke_gate_passed",
            f"{report.complete} of {len(question_ids)} smoke questions completed "
            f"({report.void} void, {report.error} error); the gate stays shut",
        )

    gate = write_smoke_gate(args.results_dir / SMOKE_GATE_FILENAME, question_ids=question_ids)
    print(f"smoke gate         {gate}")
    print("--phase full-s is now permitted")
    return EXIT_OK


async def _question_run(args: argparse.Namespace) -> int:
    """Run the named question(s) end to end and show what each one did."""
    expected_sha256, revision = _require_dataset_pin(args)
    dataset_path: Path = args.dataset or _default_dataset(QUESTION_DEFAULT_PHASE)
    dataset = _load(dataset_path, expected_sha256=expected_sha256, revision=revision)
    questions = select_questions(dataset, question_ids=args.question_id)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir: Path = args.results_dir / QUESTION_DIRNAME / stamp
    results_path = run_dir / QUESTION_RESULTS_FILENAME

    # Labelled oracle because that is the corpus it reads by default, and every
    # phase but sample-s runs whatever question set it is handed — so handing
    # PhaseRunner the subset IS the selection, exactly as --smoke does it. Which
    # corpus was actually read is not left to the label: the manifest names the
    # file and pins its SHA-256.
    report = await _execute(
        phase=QUESTION_DEFAULT_PHASE,
        dataset=dataset.model_copy(update={"questions": questions}),
        dataset_path=dataset_path,
        expected_sha256=expected_sha256,
        revision=revision,
        strategy=None,
        runs_root=args.runs_root,
        results_dir=run_dir,
        results_path=results_path,
        keep_workspace_on_failure=args.keep_workspace_on_failure,
        pricing_version=args.pricing_table_version,
    )
    _print_report(report, results_path=results_path)
    _print_question_results(results_path, dataset=dataset)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _print_estimate(estimate: Estimate, *, phase: Phase, dataset_path: Path) -> None:
    print(f"phase              {phase} (dry run — nothing was called)")
    print(f"dataset            {dataset_path}")
    print(f"pricing table      {estimate.pricing_table_version}")
    print(f"questions          {estimate.n_questions}")
    print(f"voided questions   {estimate.n_voided_questions}")
    print(f"sessions           {estimate.n_sessions}")
    print(f"chunks             {estimate.n_chunks}")
    print(f"llm calls          {estimate.n_calls}")
    print(f"tokens in          {estimate.tokens_in}")
    print(f"tokens out         {estimate.tokens_out}")
    print(f"estimated cost     ${estimate.cost_usd:,.2f}")
    print(f"abort ceiling      ${estimate.cost_usd * CEILING_FRACTION:,.2f}")


def _print_report(report: PhaseReport, *, results_path: Path) -> None:
    print(f"phase              {report.phase}")
    print(f"requested          {report.requested}")
    print(f"skipped (resume)   {report.skipped}")
    print(f"complete           {report.complete}")
    print(f"void               {report.void}")
    print(f"error              {report.error}")
    print(f"wall seconds       {report.wall_seconds:.1f}")
    print(f"results            {results_path}")
    if report.stratification is not None:
        print(f"stratification     {report.stratification.strategy}")
        for name, count in report.stratification.per_type.items():
            print(f"  {name:<28} {count}")


def _print_question_results(results_path: Path, *, dataset: Dataset) -> None:
    """Show what each question answered, read back from the rows that were written.

    Read back rather than reported from memory: the row is what a later analysis
    will score, so a run that prints one thing and records another is a bug this
    is positioned to catch rather than hide.
    """
    asked = {str(raw["question_id"]): raw for raw in dataset.questions}
    for line in results_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = ResultRow.model_validate(json.loads(line))
        raw = asked.get(row.question_id, {})
        print()
        print(f"question           {row.question_id} ({row.question_type})")
        print(f"asked              {raw.get('question', '')}")
        print(f"gold answer        {raw.get('answer', '')}")
        print(f"agent answer       {row.answer or '(none)'}")
        print(f"judge verdict      {_verdict_line(row)}")


def _verdict_line(row: ResultRow) -> str:
    """The judge's call on one row, or why that row never reached the judge."""
    if row.verdict is not None:
        return "CORRECT" if row.verdict.get("correct") else "INCORRECT"
    if row.void_reason is not None:
        return f"none — question voided ({row.void_reason})"
    return f"none — status {row.status}"


def _abort(code: int, assertion: str, detail: str) -> int:
    """Name the assertion that failed, then hand back its exit code."""
    print(f"ABORT [{assertion}] {detail}", file=sys.stderr)
    return code


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _dispatch(args: argparse.Namespace) -> int:
    if args.dry_run:
        return _dry_run(args)
    if args.question_id:
        return await _question_run(args)
    if args.smoke is not None:
        return await _smoke(args)
    return await _phase(args)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse, dispatch, and turn every phase-level abort into a named exit code."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    _check_usage(parser, args)

    try:
        return asyncio.run(_dispatch(args))
    except PreflightError as exc:
        return _abort(EXIT_PREFLIGHT, exc.assertion, str(exc))
    except RepoHygieneError as exc:
        return _abort(EXIT_HYGIENE, "gitignored_artifacts", str(exc))
    except SpendCeilingExceeded as exc:
        return _abort(EXIT_CEILING, "spend_ceiling", str(exc))
    except SmokeGateError as exc:
        return _abort(EXIT_SMOKE_GATE, "smoke_gate_passed", str(exc))
    except (
        DatasetUnavailableError,
        DatasetPinRequiredError,
        QuestionNotFoundError,
        ResumeRefusedError,
    ) as exc:
        return _abort(EXIT_ERROR, type(exc).__name__, str(exc))


__all__ = [
    # Re-exported from evaluations.longmemeval.paths, which owns them. Named
    # here because the CLI is where they are the *defaults* a run resolves
    # against, and tests patch them on this module to redirect a whole run.
    "DATA_DIR",
    "EXIT_CEILING",
    "EXIT_ERROR",
    "EXIT_HYGIENE",
    "EXIT_OK",
    "EXIT_PREFLIGHT",
    "EXIT_SMOKE_GATE",
    "PHASE_DATASETS",
    "QUESTION_DEFAULT_PHASE",
    "QUESTION_DIRNAME",
    "QUESTION_RESULTS_FILENAME",
    "RESULTS_DIR",
    "RUNS_ROOT",
    "SMOKE_GATE_FILENAME",
    "SMOKE_MAX",
    "SMOKE_MIN",
    "DatasetPinRequiredError",
    "ResumeRefusedError",
    "SmokeGateError",
    "assert_smoke_gate_passed",
    "build_parser",
    "main",
    "resolved_model_ids",
    "select_questions",
    "select_smoke_questions",
    "write_smoke_gate",
]


if __name__ == "__main__":
    raise SystemExit(main())
