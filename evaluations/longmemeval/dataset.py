"""DatasetLoader (COMP-003) — raw-byte SHA-256 gate over a LongMemEval dataset file.

The dataset is a third-party download that feeds a memory store, so its bytes are
untrusted until pinned (REQ-201, LLM04). The same hash is what distinguishes the
Sept-2025 'cleaned' revision from the original, whose scores are not numerically
comparable to it (REQ-175).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class DatasetIntegrityError(Exception):
    """The dataset's raw bytes do not hash to the pinned SHA-256."""


class Dataset(BaseModel):
    """A loaded dataset plus the provenance a run manifest has to record."""

    questions: list[dict[str, Any]]
    sha256: str
    revision: str


def load_dataset(
    path: Path,
    *,
    expected_sha256: str | None = None,
    revision: str,
) -> Dataset:
    """Load `longmemeval_oracle.json` / `longmemeval_s_cleaned.json` under a hash pin.

    The digest covers the raw file bytes rather than the re-serialized structure, so
    formatting drift the pin exists to catch cannot slip through. A mismatch raises
    before the JSON is parsed — a poisoned or drifted file must never reach a caller
    that could score against it.
    """
    raw = path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()

    if expected_sha256 is not None and sha256 != expected_sha256:
        raise DatasetIntegrityError(
            f"{path} hashes to {sha256}, expected {expected_sha256}; refusing to load."
        )

    return Dataset(questions=json.loads(raw), sha256=sha256, revision=revision)
