"""ResultLedger (COMP-018) — append-only JSONL whose atomic unit is the question.

A row is written once, after ingest, query and judge have *all* finished, and it
carries a terminal ``status`` (REQ-204). Nothing partway through a question is
ever recorded, because chunk-level LLM calls are order-sensitive and not
idempotent: a half-described question must come back as not done so the phase
rebuilds it from scratch rather than scoring a workspace missing chunks.

The file is only ever appended to and never read-modify-written (REQ-206). The
done-set is built by one full read at startup; after that the write handle never
seeks, so a crash can truncate at most the line in flight. That truncated final
line is the *normal* SIGKILL signature, not corruption — it loads cleanly and its
question simply counts as not done.

Rows are self-describing on purpose: `provenance` is the full COMP-016 block
repeated on every line (REQ-210), so results from several runs stay
interpretable after the files are concatenated.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, TextIO

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

DONE_STATUS = "complete"
"""The only status that counts as done. ``void`` and ``error`` are re-derived on
resume: a void verdict is reached before any spend, and an errored question must
be retried, never silently accepted as measured."""

DEFAULT_FSYNC_EVERY = 10
"""Completions between ``fsync()`` calls. Every row is ``flush()``ed regardless;
this bounds how many rows the OS page cache can lose to a machine-level crash."""


class ResultRow(BaseModel):
    """One completed question, exactly as it lands on a line of the results file.

    The composite blocks — ``verdict``, ``retrieval``, ``cost``, ``provenance`` —
    are opaque mappings rather than typed models. Each is owned by a different
    component (COMP-010, COMP-012, COMP-021, COMP-016); the ledger's job is to
    carry them through byte-faithfully, and forking their schemas here would put
    two definitions of the same block in the repository.

    Everything a row cannot always know is optional, so an ``error`` row can be
    written from a question that failed before it had an answer, a date or a
    cost. Only what is knowable before any work starts is required.
    """

    model_config = ConfigDict(frozen=True)

    question_id: str
    status: Literal["complete", "void", "error"]
    void_reason: Literal["gold_evidence_filtered", "turn_exceeds_cap"] | None = None
    question_type: str
    is_abstention: bool
    answer: str = ""
    verdict: dict[str, Any] | None = None
    prior_labels: list[dict[str, Any]] = Field(default_factory=list)
    retrieval: dict[str, Any] | None = None
    question_date: date | None = None
    question_date_source: Literal["dataset", "derived"] | None = None
    cost: dict[str, Any] | None = None
    provenance: dict[str, Any]


class ResultLedger:
    """Append-only writer over the results JSONL, plus the resume done-set."""

    def __init__(self, results_path: Path, *, fsync_every: int = DEFAULT_FSYNC_EVERY) -> None:
        if fsync_every < 1:
            raise ValueError(f"fsync_every must be at least 1, got {fsync_every}")

        self._path = results_path
        self._fsync_every = fsync_every
        self._appends_since_fsync = 0
        self._closed = False

        results_path.parent.mkdir(parents=True, exist_ok=True)
        self._done, self._line_unterminated = _scan(results_path)
        self._handle: TextIO = results_path.open("a", encoding="utf-8", newline="\n")

    def done_set(self) -> set[str]:
        """Question ids already recorded complete — a copy, so no caller can forge one.

        Built by the single startup scan and extended by this process's own
        appends. It is never rebuilt from the file, so a row another writer lands
        mid-phase is invisible here by design.
        """
        return set(self._done)

    def append(self, row: ResultRow) -> None:
        """Write one terminal row and make it durable."""
        if self._line_unterminated:
            # A previous run died mid-line. Close that line off before adding to
            # it, or the partial row and this one fuse into a single unparseable
            # line and *both* questions are lost. Still append-only.
            self._handle.write("\n")
            self._line_unterminated = False

        self._handle.write(json.dumps(row.model_dump(mode="json"), ensure_ascii=False) + "\n")
        self._handle.flush()

        self._appends_since_fsync += 1
        if self._appends_since_fsync >= self._fsync_every:
            self._sync()

        if row.status == DONE_STATUS:
            self._done.add(row.question_id)

    def close(self) -> None:
        """Flush, sync the tail and release the handle. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self._handle.flush()
        self._sync()
        self._handle.close()

    def _sync(self) -> None:
        os.fsync(self._handle.fileno())
        self._appends_since_fsync = 0

    def __enter__(self) -> ResultLedger:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _scan(path: Path) -> tuple[set[str], bool]:
    """Read the results file once: the completed ids, and whether it ends mid-line.

    ``errors='replace'`` because a byte-level truncation can land inside a
    multi-byte UTF-8 sequence; a resume must not die decoding the very crash it
    exists to recover from.
    """
    if not path.exists():
        return set(), False

    done: set[str] = set()
    unterminated = False
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for number, raw in enumerate(handle, start=1):
            unterminated = not raw.endswith("\n")
            question_id = _completed_id(raw, number)
            if question_id is not None:
                done.add(question_id)
    return done, unterminated


def _completed_id(raw: str, number: int) -> str | None:
    """The question id this line proves is done, or ``None`` if it proves nothing."""
    line = raw.strip()
    if not line:
        return None

    try:
        row = ResultRow.model_validate(json.loads(line))
    except (json.JSONDecodeError, ValidationError):
        logger.warning(
            "results line %d is partial or unparseable; its question counts as not done",
            number,
        )
        return None

    return row.question_id if row.status == DONE_STATUS else None
