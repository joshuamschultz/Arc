# ruff: noqa: T201 — CLI tool; print is the right primitive here.
"""Dataset-wide sanitize-damage report (T-795 / COMP-005 / REQ-180, REQ-181).

Answers the one question that decides whether any score is trustworthy at all:
how often do the live arcmemory filters destroy a question's gold evidence
before it can ever become memory? A question whose evidence was filtered on the
way in produces a real-looking low score for a crippled input path, so this rate
is a ceiling on what the benchmark can measure — and it has to be known BEFORE
the Oracle phase spends anything.

Runs the real corpus through the real ``TurnChunker`` and the real
``SanitizeFidelityGate``, which in turn calls the real ``sanitize`` and
``privacy_filter``. Nothing here re-derives filter behavior.

The dataset is a manual, gitignored download (``evaluations/data/``), so the
script names the missing file and exits rather than fabricating a corpus.

Run it from the repository root — ``evaluations`` is deliberately not an
installed workspace member, so the package must be importable from the cwd::

    uv run python -m evaluations.longmemeval.damage_report
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from evaluations.ingest.chunker import TurnChunker, TurnExceedsCapError
from evaluations.ingest.fidelity import GoldEvidenceFilteredError, SanitizeFidelityGate
from evaluations.ingest.types import Session, Turn

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_DEFAULT_DATASET = _DATA_DIR / "longmemeval_oracle.json"
_DATASET_HOME = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned"

# Dataset dates read "2023/05/20 (Sat) 02:29" — only the leading day is needed.
_DATE_FORMAT = "%Y/%m/%d"

_SAMPLE_LIMIT = 10
_SAMPLE_CHARS = 90


@dataclass(frozen=True)
class QuestionDamage:
    """What the live filters did to one question's haystack."""

    chunks: int
    damaged_chunks: int
    chars_destroyed: int
    gold_hit: bool
    refused_sessions: list[str]
    samples: list[str]


@dataclass(frozen=True)
class Report:
    """What one pass over the corpus observed."""

    questions: int
    gold_damaged_questions: int
    refused_questions: int
    chunks: int
    damaged_chunks: int
    chars_destroyed: int
    refused_sessions: list[str]
    samples: list[str]


def main() -> int:
    """Report the corpus-wide gold-damage rate; non-zero if the dataset is absent."""
    args = _parse_args()
    dataset_path: Path = args.dataset

    if not dataset_path.is_file():
        print(f"Dataset not found: {dataset_path}")
        print(f"It is a manual, gitignored download. Fetch it from {_DATASET_HOME}")
        print(f"and place the .json file in {_DATA_DIR}/ , then re-run this script.")
        return 1

    questions: list[dict[str, Any]] = json.loads(dataset_path.read_bytes())
    chunker = TurnChunker(max_event_chars=args.max_event_chars, target=args.target)
    gate = SanitizeFidelityGate(max_event_chars=args.max_event_chars)
    report = _scan(questions, chunker, gate)
    _print(report, dataset_path, max_event_chars=args.max_event_chars, target=args.target)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=_DEFAULT_DATASET)
    parser.add_argument("--max-event-chars", type=int, default=2000)
    parser.add_argument("--target", type=int, default=1700)
    return parser.parse_args()


def _scan(
    questions: list[dict[str, Any]],
    chunker: TurnChunker,
    gate: SanitizeFidelityGate,
) -> Report:
    """Walk every chunk of every question through the gate."""
    per_question = [_scan_question(question, chunker, gate) for question in questions]
    samples: list[str] = []
    for damage in per_question:
        samples.extend(damage.samples)

    return Report(
        questions=len(per_question),
        gold_damaged_questions=sum(1 for damage in per_question if damage.gold_hit),
        refused_questions=sum(1 for damage in per_question if damage.refused_sessions),
        chunks=sum(damage.chunks for damage in per_question),
        damaged_chunks=sum(damage.damaged_chunks for damage in per_question),
        chars_destroyed=sum(damage.chars_destroyed for damage in per_question),
        refused_sessions=[label for damage in per_question for label in damage.refused_sessions],
        samples=samples[:_SAMPLE_LIMIT],
    )


def _scan_question(
    question: dict[str, Any],
    chunker: TurnChunker,
    gate: SanitizeFidelityGate,
) -> QuestionDamage:
    """Check one question's whole haystack, chunk by chunk."""
    gold_turn_ids = _gold_turn_ids(question)
    chunks = 0
    damaged_chunks = 0
    chars_destroyed = 0
    gold_hit = False
    refused: list[str] = []
    samples: list[str] = []

    for session_idx, session in enumerate(_sessions(question)):
        try:
            session_chunks = chunker.split(session, session_idx=session_idx)
        except TurnExceedsCapError as exc:
            # Voids under REQ-177 instead, and its text never reaches the gate.
            refused.append(f"{session.conversation_id} ({exc.turn_id})")
            continue
        for chunk in session_chunks:
            chunks += 1
            verdict = gate.check(chunk, gold_turn_ids=gold_turn_ids)
            if verdict.ok:
                continue
            damaged_chunks += 1
            chars_destroyed += verdict.shrunk_by
            if verdict.gold_overlap:
                gold_hit = True
                samples.append(_sample(question["question_id"], chunk.text, verdict.spans))

    return QuestionDamage(
        chunks=chunks,
        damaged_chunks=damaged_chunks,
        chars_destroyed=chars_destroyed,
        gold_hit=gold_hit,
        refused_sessions=refused,
        samples=samples[:_SAMPLE_LIMIT],
    )


def _gold_turn_ids(question: dict[str, Any]) -> set[str]:
    """Turn ids the question's gold evidence names (REQ-181).

    ``has_answer`` flags the evidence turns directly. A session named by
    ``answer_session_ids`` that flags no turn falls back to all of its turns:
    attributing damage too widely voids a question that might have scored, which
    is the safe direction to be wrong in.
    """
    question_id: str = question["question_id"]
    answer_sessions = set(question["answer_session_ids"])
    sessions: list[list[dict[str, Any]]] = question["haystack_sessions"]
    session_ids: list[str] = question["haystack_session_ids"]

    gold: set[str] = set()
    for session_idx, (session_id, turns) in enumerate(zip(session_ids, sessions, strict=True)):
        flagged = {
            f"{question_id}#{session_idx}#{turn_idx}"
            for turn_idx, turn in enumerate(turns)
            if turn.get("has_answer")
        }
        if not flagged and session_id in answer_sessions:
            flagged = {f"{question_id}#{session_idx}#{i}" for i in range(len(turns))}
        gold |= flagged
    return gold


def _sessions(question: dict[str, Any]) -> Iterator[Session]:
    """Yield one ``Session`` per haystack session, in the dataset's own order.

    Turn ids match ``_gold_turn_ids`` above and are local to this report — COMP-002
    owns the scheme the scored run uses; nothing here is written to memory.
    """
    question_id: str = question["question_id"]
    sessions: list[list[dict[str, Any]]] = question["haystack_sessions"]
    dates: list[str] = question["haystack_dates"]

    for session_idx, (raw_date, turns) in enumerate(zip(dates, sessions, strict=True)):
        yield Session(
            conversation_id=f"{question_id}#{session_idx}",
            source_date=datetime.strptime(raw_date.split()[0], _DATE_FORMAT).date(),
            turns=[
                Turn(
                    turn_id=f"{question_id}#{session_idx}#{turn_idx}",
                    role=turn["role"],
                    text=turn["content"],
                )
                for turn_idx, turn in enumerate(turns)
            ],
        )


def _sample(question_id: str, text: str, spans: list[tuple[int, int]]) -> str:
    """One line naming a question and the gold text the filters destroyed."""
    destroyed = " | ".join(text[start:stop] for start, stop in spans)
    if len(destroyed) > _SAMPLE_CHARS:
        destroyed = f"{destroyed[:_SAMPLE_CHARS]}..."
    return f"{question_id}: {destroyed!r}"


def _print(
    report: Report,
    dataset_path: Path,
    *,
    max_event_chars: int,
    target: int,
) -> None:
    """Print the damage rates and what they mean for the run about to start."""
    print(f"dataset            {dataset_path}")
    print(f"max_event_chars    {max_event_chars}   target {target}")
    print(f"questions          {report.questions}")
    print(f"chunks checked     {report.chunks}")

    if not report.questions:
        print("\nNo questions found — nothing to check.")
        return

    damaged_rate = report.damaged_chunks / report.chunks if report.chunks else 0.0
    gold_rate = report.gold_damaged_questions / report.questions

    print("\nfilter damage")
    print(f"  chunks damaged   {report.damaged_chunks} of {report.chunks} ({damaged_rate:.3%})")
    print(f"  characters lost  {report.chars_destroyed}")

    print("\ngold-evidence damage (REQ-181)")
    print(
        f"  questions voided {report.gold_damaged_questions} of {report.questions} "
        f"({gold_rate:.3%})   reason {GoldEvidenceFilteredError.reason}"
    )
    for line in report.samples:
        print(f"    {line}")

    refused = len(report.refused_sessions)
    print(
        f"\nsessions the chunker refuses {refused} across {report.refused_questions} question(s)"
    )
    for label in report.refused_sessions[:_SAMPLE_LIMIT]:
        print(f"    {label}")
    if refused > _SAMPLE_LIMIT:
        print(f"    ... and {refused - _SAMPLE_LIMIT} more")

    verdict = (
        "no gold evidence is destroyed on the way in — the filters do not cap the score"
        if not report.gold_damaged_questions
        else (
            f"{gold_rate:.3%} of questions cannot be scored at all; every remaining "
            "number is measured on the rest"
        )
    )
    print(f"\ntrustworthiness: {verdict}")


if __name__ == "__main__":
    raise SystemExit(main())
