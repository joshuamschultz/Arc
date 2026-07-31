# ruff: noqa: T201 — CLI tool; print is the right primitive here.
"""Turn-length measurement over the real LongMemEval corpus (T-786 / COMP-004).

Answers one question before any spend is committed: is COMP-004's "never split
mid-turn" rule achievable on this dataset at all? If even a handful of haystack
turns overflow ``max_event_chars`` on their own, those questions void under
REQ-177 and the achievable ceiling of the benchmark drops before a single LLM
call is made.

Runs the real corpus through the real ``TurnChunker`` rather than a re-derived
length model, so what it reports is what the harness will actually do.

The dataset is a manual, gitignored download (``evaluations/data/``), so the
script names the missing file and exits rather than fabricating a corpus.

Run it from the repository root — ``evaluations`` is deliberately not an
installed workspace member, so the package must be importable from the cwd::

    python -m evaluations.longmemeval.measure_turn_lengths
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
from evaluations.ingest.types import Session, Turn

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_DEFAULT_DATASET = _DATA_DIR / "longmemeval_oracle.json"
_DATASET_HOME = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned"

# Dataset dates read "2023/05/20 (Sat) 02:29" — only the leading day is needed.
_DATE_FORMAT = "%Y/%m/%d"


@dataclass(frozen=True)
class Measurement:
    """What one pass over the corpus observed."""

    questions: int
    turn_lengths: list[int]
    chunks_per_session: list[int]
    over_cap_turns: list[str]
    refused_sessions: list[str]


def main() -> int:
    """Measure the corpus and print the distribution; non-zero if it is absent."""
    args = _parse_args()
    dataset_path: Path = args.dataset

    if not dataset_path.is_file():
        print(f"Dataset not found: {dataset_path}")
        print(f"It is a manual, gitignored download. Fetch it from {_DATASET_HOME}")
        print(f"and place the .json file in {_DATA_DIR}/ , then re-run this script.")
        return 1

    questions: list[dict[str, Any]] = json.loads(dataset_path.read_bytes())
    chunker = TurnChunker(max_event_chars=args.max_event_chars, target=args.target)
    measurement = _measure(questions, chunker, max_event_chars=args.max_event_chars)
    _report(measurement, dataset_path, max_event_chars=args.max_event_chars, target=args.target)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=_DEFAULT_DATASET)
    parser.add_argument("--max-event-chars", type=int, default=2000)
    parser.add_argument("--target", type=int, default=1700)
    return parser.parse_args()


def _measure(
    questions: list[dict[str, Any]],
    chunker: TurnChunker,
    *,
    max_event_chars: int,
) -> Measurement:
    """Walk every session of every question through the real chunker."""
    turn_lengths: list[int] = []
    chunks_per_session: list[int] = []
    over_cap_turns: list[str] = []
    refused_sessions: list[str] = []

    for question in questions:
        for session_idx, session in enumerate(_sessions(question)):
            for turn in session.turns:
                turn_lengths.append(len(turn.text))
                if len(turn.text) > max_event_chars:
                    over_cap_turns.append(turn.turn_id)
            try:
                chunks_per_session.append(len(chunker.split(session, session_idx=session_idx)))
            except TurnExceedsCapError as exc:
                refused_sessions.append(f"{session.conversation_id} ({exc.turn_id})")

    return Measurement(
        questions=len(questions),
        turn_lengths=turn_lengths,
        chunks_per_session=chunks_per_session,
        over_cap_turns=over_cap_turns,
        refused_sessions=refused_sessions,
    )


def _sessions(question: dict[str, Any]) -> Iterator[Session]:
    """Yield one ``Session`` per haystack session, in the dataset's own order.

    Turn ids are local to this measurement — COMP-002 owns the scheme the scored
    run uses; nothing here is written to memory or compared to gold evidence.
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


def _percentile(sorted_values: list[int], fraction: float) -> int:
    """Nearest-rank percentile of an already-sorted, non-empty list."""
    rank = min(len(sorted_values) - 1, int(fraction * len(sorted_values)))
    return sorted_values[rank]


def _report(
    measurement: Measurement,
    dataset_path: Path,
    *,
    max_event_chars: int,
    target: int,
) -> None:
    """Print the distribution and the achievability verdict."""
    lengths = sorted(measurement.turn_lengths)
    total = len(lengths)
    sessions = len(measurement.chunks_per_session) + len(measurement.refused_sessions)

    print(f"dataset            {dataset_path}")
    print(f"max_event_chars    {max_event_chars}   target {target}")
    print(f"questions          {measurement.questions}")
    print(f"sessions           {sessions}")
    print(f"turns              {total}")

    if not total:
        print("\nNo turns found — nothing to measure.")
        return

    print("\nturn length (characters)")
    print(f"  min              {lengths[0]}")
    print(f"  p50              {_percentile(lengths, 0.50)}")
    print(f"  p90              {_percentile(lengths, 0.90)}")
    print(f"  p95              {_percentile(lengths, 0.95)}")
    print(f"  p99              {_percentile(lengths, 0.99)}")
    print(f"  max              {lengths[-1]}")
    print(f"  mean             {sum(lengths) / total:.1f}")

    over_cap = len(measurement.over_cap_turns)
    print(f"\nturns over max_event_chars   {over_cap} of {total} ({over_cap / total:.3%})")
    for turn_id in measurement.over_cap_turns[:10]:
        print(f"  {turn_id}")
    if over_cap > 10:
        print(f"  ... and {over_cap - 10} more")

    refused = len(measurement.refused_sessions)
    print(f"\nsessions the chunker refuses {refused}")
    for label in measurement.refused_sessions[:10]:
        print(f"  {label}")
    if refused > 10:
        print(f"  ... and {refused - 10} more")

    chunks = measurement.chunks_per_session
    if chunks:
        print("\nchunks per session")
        print(f"  total            {sum(chunks)}")
        print(f"  mean             {sum(chunks) / len(chunks):.2f}")
        print(f"  max              {max(chunks)}")

    verdict = (
        "achievable — no turn overflows the cap on its own"
        if not refused
        else f"NOT achievable for {refused} session(s); those questions void under REQ-177"
    )
    print(f"\nnever-split-mid-turn: {verdict}")


if __name__ == "__main__":
    raise SystemExit(main())
