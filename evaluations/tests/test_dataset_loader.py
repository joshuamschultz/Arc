"""DatasetLoader (COMP-003) — raw-byte SHA-256 gate and HF revision carry-through.

The hash is the LLM04 guard on a third-party download that feeds a memory store
(REQ-201), and the only way to tell the Sept-2025 'cleaned' revision apart from
the original, which is not numerically comparable to it (REQ-175).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evaluations.longmemeval.dataset import DatasetIntegrityError, load_dataset

# Hand-written LongMemEval-shaped fixture. Never the real dataset — that lives in
# gitignored evaluations/data/ and is far too large to commit.
_FIXTURE = Path(__file__).parent / "fixtures" / "longmemeval_fixture.json"

_REVISION = "1d0f1a5c9e3b47a28f6c05d9b7e4a3128c9f6b02"


def _raw_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_correct_expected_sha256_loads_and_exposes_questions() -> None:
    dataset = load_dataset(_FIXTURE, expected_sha256=_raw_digest(_FIXTURE), revision=_REVISION)

    assert [q["question_id"] for q in dataset.questions] == [
        "fixture_single_session_user_1",
        "fixture_knowledge_update_2_abs",
    ]
    assert dataset.sha256 == _raw_digest(_FIXTURE)
    assert dataset.revision == _REVISION


def test_wrong_expected_sha256_raises_dataset_integrity_error() -> None:
    with pytest.raises(DatasetIntegrityError):
        load_dataset(_FIXTURE, expected_sha256="0" * 64, revision=_REVISION)


def test_mutated_file_fails_against_the_pinned_hash(tmp_path: Path) -> None:
    """A single flipped gold answer must abort the load, not silently score against it."""
    pinned = _raw_digest(_FIXTURE)
    tampered = tmp_path / "longmemeval_tampered.json"
    tampered.write_bytes(_FIXTURE.read_bytes().replace(b"Bear Gulch Trail", b"Bear Creek Trail"))

    with pytest.raises(DatasetIntegrityError):
        load_dataset(tampered, expected_sha256=pinned, revision=_REVISION)


def test_sha256_is_computed_over_raw_file_bytes_not_reserialized_json() -> None:
    """Hashing the parsed structure would drop formatting drift the pin exists to catch."""
    dataset = load_dataset(_FIXTURE, revision=_REVISION)

    reserialized = hashlib.sha256(json.dumps(dataset.questions).encode("utf-8")).hexdigest()
    assert dataset.sha256 == _raw_digest(_FIXTURE)
    assert dataset.sha256 != reserialized


@pytest.mark.parametrize("revision", ["main", "9c4e2f18ab7d0356e1f9c8b24d5a7069f3e18b4a"])
def test_revision_is_carried_through_verbatim(revision: str) -> None:
    dataset = load_dataset(_FIXTURE, expected_sha256=_raw_digest(_FIXTURE), revision=revision)

    assert dataset.revision == revision
