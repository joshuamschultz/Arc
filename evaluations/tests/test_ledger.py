"""ResultLedger (COMP-018) — resume tolerance, append-only durability, done-set.

The behaviours under test are the ones a SIGKILL exercises: a half-written final
line must load cleanly rather than raise (REQ-206), and a question whose row is
missing, partial or unparseable must come back as not done so the run rebuilds it
(REQ-204). Everything here writes inside ``tmp_path`` and makes no network call.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from evaluations.longmemeval.ledger import ResultLedger, ResultRow

# An opaque COMP-016 block. The ledger carries provenance through verbatim and
# never imports the manifest module, so a plain mapping is the honest stand-in.
_PROVENANCE: dict[str, Any] = {
    "git_sha": "0f1e2d3c4b5a69788796a5b4c3d2e1f0aabbccdd",
    "git_dirty": False,
    "harness_version": "0.1.0",
    "config_hash": "9a" * 32,
    "dataset_sha256": "3c" * 32,
    "agent_model_id": "openai/gpt-4o-2024-08-06",
    "judge_model_id": "openai/gpt-4o-2024-08-06",
    "tier": "personal",
    "run_timestamp_utc": "2026-07-30T12:00:00Z",
    "question_id": "q1",
}


def _row(question_id: str, **overrides: Any) -> ResultRow:
    fields: dict[str, Any] = {
        "question_id": question_id,
        "status": "complete",
        "question_type": "single-session-user",
        "is_abstention": False,
        "answer": "Sourdough, since March.",
        "verdict": {
            "correct": True,
            "raw_response": "yes",
            "prompt_used": "…",
            "judge_model_id": "gpt-4o-2024-08-06",
        },
        "retrieval": {
            "turn": {"any@k": 1.0, "all@k": 0.5},
            "session": {"any@k": 1.0, "all@k": 1.0},
        },
        "question_date": date(2023, 5, 20),
        "question_date_source": "dataset",
        "cost": {
            "tokens_in": 1200,
            "tokens_out": 40,
            "cost_usd": 0.013,
            "n_llm_calls": 9,
            "wall_seconds": 61.4,
        },
        "provenance": _PROVENANCE | {"question_id": question_id},
    }
    return ResultRow(**(fields | overrides))


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_appended_row_is_one_json_line_and_survives_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"

    with ResultLedger(path) as writing:
        writing.append(_row("q1"))

    assert len(_lines(path)) == 1
    assert json.loads(_lines(path)[0])["question_id"] == "q1"
    assert ResultLedger(path).done_set() == {"q1"}


def test_row_round_trips_dates_and_carries_provenance_verbatim(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"

    with ResultLedger(path) as writing:
        writing.append(_row("q1"))

    written = json.loads(_lines(path)[0])
    assert written["question_date"] == "2023-05-20"
    assert written["question_date_source"] == "dataset"
    assert written["provenance"] == _PROVENANCE
    assert written["cost"]["n_llm_calls"] == 9


def test_only_complete_rows_are_done_void_and_error_are_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"

    with ResultLedger(path) as writing:
        writing.append(_row("q1"))
        writing.append(_row("q2", status="void", void_reason="gold_evidence_filtered"))
        writing.append(_row("q3", status="error"))

    assert ResultLedger(path).done_set() == {"q1"}


def test_truncated_final_line_loads_cleanly_and_counts_as_not_done(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    with ResultLedger(path) as writing:
        writing.append(_row("q1"))
        writing.append(_row("q2"))

    # The normal SIGKILL signature: the last line stops mid-write, no newline.
    whole = path.read_text(encoding="utf-8")
    first_line = whole[: whole.index("\n") + 1]
    path.write_text(first_line + '{"question_id": "q2", "sta', encoding="utf-8")

    assert ResultLedger(path).done_set() == {"q1"}


def test_truncation_inside_a_multibyte_character_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    with ResultLedger(path) as writing:
        writing.append(_row("q1"))

    # A byte-level cut can land mid-UTF-8-sequence; decoding must not explode.
    raw = path.read_bytes() + '{"question_id": "q2", "answer": "café'.encode()
    path.write_bytes(raw[:-1])

    assert ResultLedger(path).done_set() == {"q1"}


def test_unparseable_row_counts_as_not_done_rather_than_raising(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    with ResultLedger(path) as writing:
        writing.append(_row("q1"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n")

    assert ResultLedger(path).done_set() == {"q1"}


def test_partial_row_claiming_complete_counts_as_not_done(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    path.write_text(
        json.dumps({"question_id": "q1", "status": "complete"}) + "\n",
        encoding="utf-8",
    )

    assert ResultLedger(path).done_set() == set()


def test_missing_results_file_yields_an_empty_done_set(tmp_path: Path) -> None:
    assert ResultLedger(tmp_path / "nested" / "results.jsonl").done_set() == set()


def test_done_set_is_built_once_at_startup_and_never_re_reads_the_file(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    with ResultLedger(path) as writing:
        writing.append(_row("q1"))

    resumed = ResultLedger(path)
    # Another writer lands a row after startup. The done-set was built once, so
    # the running phase must not pick it up mid-flight.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_row("q9").model_dump(mode="json")) + "\n")

    assert resumed.done_set() == {"q1"}


def test_done_set_returns_a_copy_a_caller_cannot_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    with ResultLedger(path) as writing:
        writing.append(_row("q1"))

    resumed = ResultLedger(path)
    resumed.done_set().add("q-forged")

    assert resumed.done_set() == {"q1"}


def test_appending_after_startup_extends_the_done_set(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"

    with ResultLedger(path) as writing:
        assert writing.done_set() == set()
        writing.append(_row("q1"))
        writing.append(_row("q2", status="error"))

        assert writing.done_set() == {"q1"}


def test_append_never_rewrites_earlier_bytes(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    with ResultLedger(path) as writing:
        writing.append(_row("q1"))
    first = path.read_bytes()

    with ResultLedger(path) as resumed:
        resumed.append(_row("q2"))

    assert path.read_bytes().startswith(first)


def test_append_after_a_truncated_line_does_not_glue_onto_it(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    with ResultLedger(path) as writing:
        writing.append(_row("q1"))
    path.write_text(
        path.read_text(encoding="utf-8") + '{"question_id": "q2", "sta',
        encoding="utf-8",
    )

    with ResultLedger(path) as resumed:
        resumed.append(_row("q3"))

    assert json.loads(_lines(path)[-1])["question_id"] == "q3"
    assert ResultLedger(path).done_set() == {"q1", "q3"}


def test_every_row_is_flushed_and_fsync_lands_every_n_completions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "results.jsonl"
    fsyncs: list[int] = []
    monkeypatch.setattr(os, "fsync", fsyncs.append)

    with ResultLedger(path, fsync_every=3) as writing:
        writing.append(_row("q1"))
        writing.append(_row("q2"))
        # Flushed per line: the bytes are readable before any fsync.
        assert len(_lines(path)) == 2
        assert fsyncs == []

        writing.append(_row("q3"))
        assert len(fsyncs) == 1

        writing.append(_row("q4"))
        assert len(fsyncs) == 1

    # close() fsyncs the tail so a clean shutdown never leaves rows in the cache.
    assert len(fsyncs) == 2


def test_close_is_idempotent(tmp_path: Path) -> None:
    ledger = ResultLedger(tmp_path / "results.jsonl")
    ledger.append(_row("q1"))
    ledger.close()
    ledger.close()

    assert ResultLedger(tmp_path / "results.jsonl").done_set() == {"q1"}


def test_abstention_row_carries_no_retrieval(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"

    with ResultLedger(path) as writing:
        writing.append(_row("q1_abs", is_abstention=True, retrieval=None))

    assert json.loads(_lines(path)[0])["retrieval"] is None
    assert ResultLedger(path).done_set() == {"q1_abs"}


def test_a_non_terminal_status_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="status"):
        _row("q1", status="running")


def test_fsync_every_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fsync_every"):
        ResultLedger(tmp_path / "results.jsonl", fsync_every=0)
