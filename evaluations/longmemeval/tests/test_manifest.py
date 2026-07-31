"""RunManifest + provenance (COMP-016) — the record that makes a number evidence.

Covers T-810 (the per-row block), T-811 (`run_manifest.json` and the declared
measurement scope) and T-812 (dataset integrity and date-source fields).

Every test writes inside `tmp_path` only and makes no network call. The one
subprocess these tests run is `git`, against a throwaway repo created under
`tmp_path` — the dirty flag is worth proving against real git rather than a mock,
since a flag that never flips is the same as no flag at all.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from evaluations.longmemeval.dataset import Dataset, DatasetIntegrityError, load_dataset
from evaluations.longmemeval.manifest import (
    HARNESS_VERSION,
    MANIFEST_FILENAME,
    MEASUREMENT_SCOPE_NOTE,
    MeasurementScope,
    Provenance,
    ProvenanceError,
    RunManifest,
    build_provenance,
    config_hash,
    read_git_state,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "longmemeval_fixture.json"
_REVISION = "1d0f1a5c9e3b47a28f6c05d9b7e4a3128c9f6b02"

_RESOLVED_CONFIG: dict[str, object] = {
    "llm": {"model": "claude-opus-4-6", "api_key": "sk-live-do-not-leak"},
    "modules": {"memory": {"brain": "arcmemory", "top_k": 20}},
    "tier": "personal",
}


@pytest.fixture
def dataset() -> Dataset:
    return load_dataset(_FIXTURE, revision=_REVISION)


@pytest.fixture
def provenance(dataset: Dataset) -> Provenance:
    return build_provenance(
        resolved_config=_RESOLVED_CONFIG,
        dataset=dataset,
        agent_model_id="claude-opus-4-6",
        judge_model_id="gpt-4o-2024-08-06",
        embedder_model_id="all-MiniLM-L6-v2",
        distiller_model_id="claude-haiku-4-5",
    )


def _manifest(provenance: Provenance) -> RunManifest:
    return RunManifest(
        provenance=provenance,
        measurement_scope=MeasurementScope(workpad_enabled=True, policy_enabled=True),
        session_ingest_order={"fixture_single_session_user_1": ["s1", "s2"]},
        dry_run_estimate={
            "n_calls": 40000,
            "tokens_in": 91_000_000,
            "tokens_out": 2_000_000,
            "cost_usd": 412.5,
        },
        pricing_table_version="2026-07-01",
    )


def _git(args: list[str], cwd: Path) -> None:
    git = shutil.which("git")
    assert git is not None, "git must be on PATH for the provenance tests"
    subprocess.run([git, *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    """A throwaway repo with one commit, so HEAD resolves without touching the real one."""
    _git(["init", "-q", "-b", "main"], root)
    (root / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "tracked.txt"], root)
    _git(
        [
            "-c",
            "user.name=harness",
            "-c",
            "user.email=harness@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "initial",
        ],
        root,
    )


# --- T-810: the per-row provenance block ------------------------------------


def test_row_block_carries_every_field_a_detached_row_needs(provenance: Provenance) -> None:
    """REQ-210: a row concatenated with rows from other runs must stay self-describing."""
    row_block = provenance.for_question(
        "fixture_single_session_user_1",
        question_date_source="dataset",
        session_ingest_order=["s1", "s2"],
    ).model_dump()

    assert set(row_block) == {
        "git_sha",
        "git_dirty",
        "harness_version",
        "config_hash",
        "dataset_sha256",
        "dataset_revision",
        "agent_model_id",
        "judge_model_id",
        "embedder_model_id",
        "distiller_model_id",
        "tier",
        "question_date_source",
        "session_ingest_order",
        "run_timestamp_utc",
        "question_id",
    }
    assert all(value is not None for value in row_block.values())


def test_row_block_records_all_four_model_ids_and_the_tier(provenance: Provenance) -> None:
    """REQ-199: `MemoryConfig.for_tier` changes write and decay dynamics, so the
    number only means something with its tier and its models attached."""
    row_block = provenance.for_question(
        "q1", question_date_source="dataset", session_ingest_order=["s1"]
    )

    assert row_block.agent_model_id == "claude-opus-4-6"
    assert row_block.judge_model_id == "gpt-4o-2024-08-06"
    assert row_block.embedder_model_id == "all-MiniLM-L6-v2"
    assert row_block.distiller_model_id == "claude-haiku-4-5"
    assert row_block.tier == "personal"
    assert row_block.harness_version == HARNESS_VERSION


def test_an_empty_model_id_is_refused(dataset: Dataset) -> None:
    """A blank distiller id is the silent-degrade signature — it must not be recordable."""
    with pytest.raises(ValidationError):
        build_provenance(
            resolved_config=_RESOLVED_CONFIG,
            dataset=dataset,
            agent_model_id="claude-opus-4-6",
            judge_model_id="gpt-4o-2024-08-06",
            embedder_model_id="all-MiniLM-L6-v2",
            distiller_model_id="",
        )


def test_two_rows_of_one_run_share_the_run_facts_and_differ_only_per_question(
    provenance: Provenance,
) -> None:
    first = provenance.for_question(
        "q1", question_date_source="dataset", session_ingest_order=["s1", "s2"]
    ).model_dump()
    second = provenance.for_question(
        "q2", question_date_source="derived", session_ingest_order=["s9"]
    ).model_dump()

    per_question = {"question_id", "question_date_source", "session_ingest_order"}
    assert {k: v for k, v in first.items() if k not in per_question} == {
        k: v for k, v in second.items() if k not in per_question
    }
    assert (first["question_id"], first["session_ingest_order"]) == ("q1", ["s1", "s2"])
    assert (second["question_id"], second["session_ingest_order"]) == ("q2", ["s9"])


def test_run_timestamp_is_an_iso8601_utc_string(provenance: Provenance) -> None:
    parsed = datetime.fromisoformat(provenance.run_timestamp_utc)
    offset = parsed.utcoffset()

    assert offset is not None
    assert offset.total_seconds() == 0


# --- T-810: config hash over the RESOLVED config ----------------------------


def test_config_hash_ignores_key_order() -> None:
    """Canonicalization means a reordered dict is the same configuration."""
    reordered = {
        "tier": "personal",
        "modules": {"memory": {"top_k": 20, "brain": "arcmemory"}},
        "llm": {"api_key": "sk-live-do-not-leak", "model": "claude-opus-4-6"},
    }

    assert config_hash(reordered) == config_hash(_RESOLVED_CONFIG)


def test_config_hash_moves_when_an_env_override_repoints_a_model() -> None:
    """Hashing the config *files* would miss this: no file byte changed."""
    overridden = json.loads(json.dumps(_RESOLVED_CONFIG))
    overridden["llm"]["model"] = "claude-sonnet-5"

    assert config_hash(overridden) != config_hash(_RESOLVED_CONFIG)


def test_config_hash_preserves_list_order() -> None:
    """An ingest order is a value, not formatting — reordering it is a different run."""
    assert config_hash({"order": ["a", "b"]}) != config_hash({"order": ["b", "a"]})


def test_config_hash_survives_values_json_cannot_encode() -> None:
    assert len(config_hash({"data_dir": Path("evaluations/longmemeval/runs")})) == 64


# --- T-810: real git state --------------------------------------------------


def test_git_state_comes_from_real_git_and_flips_on_a_dirty_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)

    clean = read_git_state(tmp_path)
    (tmp_path / "tracked.txt").write_text("modified\n", encoding="utf-8")
    dirty = read_git_state(tmp_path)

    assert len(clean.sha) == 40 and int(clean.sha, 16) >= 0
    assert clean.dirty is False
    assert dirty.dirty is True
    assert dirty.sha == clean.sha


def test_git_state_raises_rather_than_recording_an_unattributable_run(tmp_path: Path) -> None:
    """No repo means no sha; a provenance block without one is not provenance."""
    not_a_repo = tmp_path / "elsewhere"
    not_a_repo.mkdir()

    with pytest.raises(ProvenanceError):
        read_git_state(not_a_repo)


def test_provenance_records_the_repo_head(provenance: Provenance) -> None:
    assert provenance.git_sha == read_git_state().sha
    assert isinstance(provenance.git_dirty, bool)


# --- T-811: run_manifest.json ----------------------------------------------


def test_manifest_declares_the_measurement_scope_in_writing(provenance: Provenance) -> None:
    """REQ-200: the scope is declared, never inferred — that is the field's whole point."""
    scope = _manifest(provenance).measurement_scope

    assert scope.workpad_enabled is True
    assert scope.policy_enabled is True
    assert scope.note.strip()
    assert "workpad" in scope.note and "policy" in scope.note
    assert "arcmemory" in scope.note
    assert scope.note == MEASUREMENT_SCOPE_NOTE


def test_a_blank_scope_note_is_refused() -> None:
    with pytest.raises(ValidationError):
        MeasurementScope(workpad_enabled=True, policy_enabled=True, note="   ")


def test_the_default_note_cannot_be_kept_when_a_module_was_disabled() -> None:
    """Otherwise the manifest would declare a scope the run did not have."""
    with pytest.raises(ValidationError):
        MeasurementScope(workpad_enabled=False, policy_enabled=True)

    honest = MeasurementScope(
        workpad_enabled=False,
        policy_enabled=True,
        note="workpad was disabled; policy was enabled.",
    )
    assert honest.workpad_enabled is False


def test_manifest_writes_the_named_file_with_every_declared_section(
    provenance: Provenance, tmp_path: Path
) -> None:
    path = _manifest(provenance).write(tmp_path)
    written = json.loads(path.read_text(encoding="utf-8"))

    assert path.name == MANIFEST_FILENAME
    assert written["provenance"]["git_sha"] == provenance.git_sha
    assert written["session_ingest_order"] == {"fixture_single_session_user_1": ["s1", "s2"]}
    assert written["dry_run_estimate"]["n_calls"] == 40000
    assert written["pricing_table_version"] == "2026-07-01"
    assert written["measurement_scope"]["note"].strip()


def test_manifest_round_trips_back_into_the_model(provenance: Provenance, tmp_path: Path) -> None:
    path = _manifest(provenance).write(tmp_path)

    reloaded = RunManifest.model_validate_json(path.read_text(encoding="utf-8"))

    assert reloaded == _manifest(provenance)


def test_manifest_records_the_hash_of_the_config_never_the_config_itself(
    provenance: Provenance, tmp_path: Path
) -> None:
    """The resolved config carries env-injected credentials (LLM07)."""
    text = _manifest(provenance).write(tmp_path).read_text(encoding="utf-8")

    assert "sk-live-do-not-leak" not in text
    assert provenance.config_hash in text


# --- T-812: dataset integrity and date source -------------------------------


def test_manifest_records_the_dataset_hash_and_hf_revision(
    provenance: Provenance, dataset: Dataset
) -> None:
    assert provenance.dataset_sha256 == hashlib.sha256(_FIXTURE.read_bytes()).hexdigest()
    assert provenance.dataset_sha256 == dataset.sha256
    assert provenance.dataset_revision == _REVISION


def test_a_dataset_that_does_not_match_the_manifest_hash_is_refused(
    provenance: Provenance, tmp_path: Path
) -> None:
    """The manifest's digest is the pin the loader enforces — one hash check, not two."""
    tampered = tmp_path / "longmemeval_tampered.json"
    tampered.write_bytes(_FIXTURE.read_bytes().replace(b"Bear Gulch Trail", b"Bear Creek Trail"))

    with pytest.raises(DatasetIntegrityError):
        load_dataset(tampered, expected_sha256=provenance.dataset_sha256, revision=_REVISION)

    reloaded = load_dataset(
        _FIXTURE, expected_sha256=provenance.dataset_sha256, revision=_REVISION
    )
    assert reloaded.sha256 == provenance.dataset_sha256


def test_question_date_source_records_which_source_supplied_the_date(
    provenance: Provenance,
) -> None:
    """REQ-215: an absent date degrades temporal reasoning into a memory failure
    that is not one, so the row says which source it used."""
    from_dataset = provenance.for_question(
        "q1", question_date_source="dataset", session_ingest_order=["s1"]
    )
    derived = provenance.for_question(
        "q2", question_date_source="derived", session_ingest_order=["s1"]
    )

    assert from_dataset.question_date_source == "dataset"
    assert derived.question_date_source == "derived"


def test_an_unknown_question_date_source_is_refused(provenance: Provenance) -> None:
    with pytest.raises(ValidationError):
        Provenance.model_validate(
            provenance.model_dump() | {"question_id": "q1", "question_date_source": "wall_clock"}
        )
