"""Where the LongMemEval corpus and its run artifacts live on disk.

One definition, because three readers need it — the CLI, the damage report and
the turn-length measurement — and a path that disagrees between them fails as
"dataset not present" while the file is sitting right there.

Every one of these is gitignored. They all sit under this package rather than
beside it: the corpus, the run workspaces and the results are LongMemEval's,
not the harness's. A second eval brings its own directory and its own
artifacts, and neither has to share a namespace with the other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

LONGMEMEVAL_ROOT: Final = Path(__file__).resolve().parent
EVALUATIONS_ROOT: Final = LONGMEMEVAL_ROOT.parent

DATA_DIR: Final = LONGMEMEVAL_ROOT / "data"
"""Manual, gitignored corpus download — `evaluations/longmemeval/data/`."""

RUNS_ROOT: Final = LONGMEMEVAL_ROOT / "runs"
"""Throwaway per-question agent workspaces. Never holds harness code."""

RESULTS_DIR: Final = LONGMEMEVAL_ROOT / "results"
"""The JSONL ledger and `run_manifest.json`."""

ORACLE_FILENAME: Final = "longmemeval_oracle.json"
S_FILENAME: Final = "longmemeval_s_cleaned.json"

__all__ = [
    "DATA_DIR",
    "EVALUATIONS_ROOT",
    "LONGMEMEVAL_ROOT",
    "ORACLE_FILENAME",
    "RESULTS_DIR",
    "RUNS_ROOT",
    "S_FILENAME",
]
